"""ConversationManager — runs the tool-loop and forwards events to a callback.

Lives on a worker thread (Qt bridge owns the QThread). The GUI never touches
this directly except through the bridge.

Cancellation: a threading.Event the GUI sets when Stop is clicked. We check
it between rounds and propagate it into client.stream() so the OpenAI iterator
short-circuits mid-chunk.

Roles: a manager instance is either a planner, a worker, or "single" (legacy
single-model chat). The role is implicit in the ToolRegistry's mode plus the
History's system prompt — the manager itself only branches when it sees a
`dispatch_to_worker` tool call: that path is intercepted and routed through
the supplied DispatchCallback rather than the registry.
"""
from __future__ import annotations

import json
import logging
import threading

_log = logging.getLogger(__name__)
from pathlib import Path
from typing import Any, Callable

from aura.client import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    TerminalOutput,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
    WorkerDispatchRequested,
)
from aura.config import ModelId, ThinkingMode
from aura.conversation import _edit_shapes
from aura.conversation._recovery_tool_policy import WORKER_RECOVERY_ALWAYS_ALLOWED, syntax_repair_tool_allowed
from aura.conversation.completion_guard import (
    assistant_message_text,
    is_completed_worker_result,
    is_repetitive_completion_final,
    terminal_result_completed,
    tool_result_completes_action,
)
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.dispatch_failure import classify_failed_worker_dispatch
from aura.conversation.edit_recovery_state import (
    default_edit_failure_class,
    edit_recovery_details,
    worker_file_state_for_path,
    worker_path_is_existing_file,
)
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.path_utils import (
    normalize_worker_path as _normalize_worker_path,
)
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.syntax_repair_state import (
    discard_syntax_validation_path,
    has_terminal_syntax_failure,
    pop_syntax_repair_state,
    set_syntax_repair_state,
    syntax_repair_paths,
    syntax_repair_state_for_path,
)
from aura.conversation.syntax_terminal_state import update_syntax_state_from_terminal
from aura.conversation.terminal_syntax import (
    is_python_path,
)
from aura.conversation.tool_limits import (
    MAX_WORKER_REDISPATCHES_PER_USER_TURN,
    WRITE_TOOLS,
    ToolLimitState,
    limit_reached_payload,
)
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_final_validation import (
    WORKER_EXPLICIT_VALIDATION_FAILURE_INSTRUCTION,
    emit_explicit_validation_result,
    run_explicit_validation_commands,
)
from aura.conversation.worker_recovery_payload import (
    blocked_tool_result,
    is_recoverable_phase_boundary,
    parse_tool_payload,
    record_recovery_block,
    recovery_payload,
)
from aura.conversation.worker_recovery_state import (
    clear_patch_failed_shapes_for_path,
    normalized_state_value,
    pop_normalized_key,
    pop_normalized_recovery_key,
    record_reads_for_recovery,
)
from aura.conversation.worker_smoothness import (
    WorkerSmoothnessState,
)
from aura.conversation.worker_smoothness import (
    note_validation_result as _note_validation_result,
)
from aura.conversation.worker_smoothness import (
    observe_tool_result as _observe_tool_result,
)
from aura.conversation.worker_smoothness import (
    phase_boundary_payload as _smoothness_phase_boundary_payload,
)
from aura.conversation.worker_smoothness import (
    precheck_tool as _precheck_tool,
)
from aura.conversation.worker_stream_buffer import WorkerStreamBuffer
from aura.conversation.worker_validation import (
    emit_auto_dependent_import_info,
    emit_auto_import_result,
    emit_auto_launch_result,
    emit_auto_py_compile_result,
    run_focused_py_compile,
)
from aura.dependency_context import compute_dependents
from aura.hooks import hooks
from aura.verify import run_dependent_import_check, run_focused_import_check

EventCallback = Callable[[Event], None]

EDIT_MECHANICS_FAILURE_CLASSES = {
    "edit_mechanics_symbol_not_found",
    "edit_mechanics_old_str_not_found",
    "edit_mechanics_ambiguous_match",
    "patch_hunk_not_found",
    "patch_hunk_ambiguous",
    "patch_file_hash_mismatch",
}

EDIT_TRANSACTION_FAILURE_CLASSES = {
    "edit_transaction_hash_mismatch",
    "edit_transaction_symbol_not_found",
    "edit_transaction_ambiguous_symbol",
    "edit_transaction_invalid_operation",
    "edit_transaction_invalid_syntax",
    "edit_transaction_not_applicable",
}

PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS = "patch_candidate_invalid_syntax"
PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS = "patch_candidate_invalid_syntax_repeated"

PATCH_CANDIDATE_INVALID_SYNTAX_ACTION = (
    "The proposed patch candidate would make this Python file invalid. The live file was not changed. "
    "Re-read the suggested range, then retry patch_file once with a larger exact old block that includes "
    "the adjacent line before and after the edit. Do not analyze patch mechanics. If the retry fails, "
    "return a concise blocker."
)

WORKER_EDIT_RECOVERY_INSTRUCTION = (
    "Previous edit failed recoverably. Re-read the affected file with read_file or read_file_range, "
    "then retry patch_file once with corrected hunks and the current expected_file_hash. "
    "Do not use write_file as a fallback for an existing-file edit. "
    "Finish only after the edit is applied and touched Python files pass py_compile."
)

WORKER_AUTO_PY_COMPILE_INSTRUCTION = (
    "Focused py_compile failed on the following Python file(s). "
    "Re-read and repair the file(s), then run python -m py_compile again. "
    "Finish only after py_compile passes.\n\n"
    "Diagnostic output:\n{diagnostics}"
)

WORKER_IMPORT_FAILURE_INSTRUCTION = (
    "Import check failed on the following Python file(s). "
    "The module(s) raised an exception on import, which means a symbol, import, "
    "or dependency is missing or broken. Re-read and repair the file(s), "
    "then finish again.\n\n"
    "Diagnostic output:\n{diagnostics}"
)

WORKER_DEPENDENT_CONTRACT_INSTRUCTION = (
    "Import check failed on downstream dependent(s) because of a contract change "
    "in the edited file(s). The edit changed a symbol, name, or signature that "
    "the following dependent(s) import. Either restore that contract in the "
    "edited file(s) or update the dependent(s) to match, then finish again.\n\n"
    "Edited: {edited_files}\n"
    "Broken dependents: {dependent_files}\n\n"
    "Diagnostic output:\n{diagnostics}"
)

WORKER_LAUNCH_FAILURE_INSTRUCTION = (
    "Launch verification failed. The declared run_command did not complete "
    "successfully: it either exited nonzero, printed a traceback/error, was "
    "cancelled, or was still running when the watch window expired. The import "
    "checks passed, so the code is structurally valid, but it fails at runtime. "
    "Re-read the relevant file(s), repair the failure shown in the output below, "
    "then finish again.\n\n"
    "Command: {command}\n\n"
    "Diagnostic output:\n{output}"
)

WORKER_SMOOTHNESS_PHASE_BOUNDARY_INSTRUCTION = (
    "Worker paused before thrashing.\n"
    "Progress made: {progress_note}\n"
    "Reason: {reason}\n"
    "Next: planner should redispatch with a narrower target or "
    "explicit validation command."
)



class ConversationManager:
    def __init__(
        self,
        history: History,
        tool_registry: ToolRegistry,
    ) -> None:
        self._history = history
        self._tools = tool_registry
        self._loop_detector = LoopDetector()
        self._tool_runner = ToolRunner(
            history=self._history,
            workspace_root=self._tools.workspace_root,
            loop_detector=self._loop_detector,
        )
        self._planner_refresh = PlannerRefreshState()

    @property
    def history(self) -> History:
        return self._history

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    def set_workspace_root(self, root: Path) -> None:
        self._tool_runner.set_workspace_root(root)

    def configure_for_planner(self, base_prompt: str, workspace_root: Path) -> None:
        """Store the base system prompt template and workspace root for mid-turn refresh."""
        self._planner_refresh.configure(base_prompt, workspace_root)

    def send(        self,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
        dispatch_cb: DispatchCallback | None = None,
        temperature: float = 0.7,
        max_tool_rounds: int | None = None,
        hook_name: str = 'generate_planner_code',
        explicit_validation_commands: list[str] | None = None,
        declared_run_command: str | None = None,
    ) -> None:
        """Run the model -> tool -> model loop until the model stops calling tools.

        Caller appends the user message to history before invoking this.

        `dispatch_cb` is required when the registry is in "planner" mode (the
        only mode that exposes the `dispatch_to_worker` tool). If the tool is
        called and `dispatch_cb` is None, the call returns an error result so
        the planner can recover rather than blocking forever.

        `hook_name` controls which hook to trigger for model generation.
        The planner uses `generate_planner_code`; workers use `generate_worker_code`.
        """
        import concurrent.futures

        reject_all_for_turn = False
        mode = getattr(self._tools, "mode", "single")
        limits = ToolLimitState(mode=mode)
        smoothness = WorkerSmoothnessState() if mode == "worker" else None
        stream_buffer = WorkerStreamBuffer() if mode == "worker" else None
        candidate_final_message: dict[str, Any] | None = None
        rounds_used = 0
        worker_needs_final_report = False
        worker_phase_boundary_info: dict[str, Any] | None = None
        worker_redispatches = 0
        worker_dispatch_failures: dict[str, int] = {}
        edit_failed_shapes: set[str] = set()
        edit_fallback_required: dict[str, dict[str, Any]] = {}
        recovery_block_counts: dict[str, int] = {}
        line_range_reread_required: dict[str, dict[str, Any]] = {}
        worker_file_state: dict[str, dict[str, Any]] = {}
        patch_failed_cycles: dict[str, int] = {}
        patch_invalid_syntax_required: dict[str, dict[str, Any]] = {}
        syntax_repair_required: dict[str, dict[str, Any]] = {}
        syntax_validation_required: set[str] = set()
        import_verification_required: set[str] = set()
        write_attempts_by_path: dict[str, int] = {}
        worker_recovery_nudge_sent = False
        stale_validation_notes: list[str] = []
        task_completion_context = False
        final_messages_after_completion = 0
        last_completion_final_text = ""

        def discard_worker_candidate_final() -> None:
            nonlocal candidate_final_message
            candidate_final_message = None
            if stream_buffer is not None:
                stream_buffer.discard()

        def smoothness_boundary_instruction(info: dict[str, Any]) -> str:
            details = info.get("details") if isinstance(info.get("details"), dict) else {}
            progress_note = str(details.get("last_progress_note") or "none recorded")
            reason = str(
                details.get("budget_reason")
                or info.get("message")
                or info.get("reason")
                or "budget reached"
            )
            return WORKER_SMOOTHNESS_PHASE_BOUNDARY_INSTRUCTION.format(
                progress_note=progress_note,
                reason=reason,
            )

        while True:
            if (
                mode in {"planner", "single"}
                and task_completion_context
                and final_messages_after_completion >= 1
            ):
                return

            rounds_used += 1
            if max_tool_rounds is not None and rounds_used > max_tool_rounds:
                on_event(ApiError(status_code=None, message=f"Exceeded max tool rounds ({max_tool_rounds})."))
                return

            limits.begin_model_round()
            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            full_message: dict[str, Any] | None = None
            tool_defs = [] if worker_needs_final_report else self._tools.tool_defs()
            if stream_buffer is not None:
                stream_buffer.begin_round()

            label = "planner_stream" if "planner" in hook_name else "worker_stream"
            _log.info(
                "%s_start model=%s thinking=%s hook_name=%s",
                label, model, thinking, hook_name,
            )
            _first_event = True

            for ev in hooks.trigger(
                hook_name,
                messages=self._history.for_api(),
                tools=tool_defs,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
                temperature=temperature,
            ):
                if _first_event:
                    _log.info("%s_first_event model=%s", label, model)
                    _first_event = False
                if mode == "worker" and stream_buffer is not None:
                    stream_buffer.capture_or_forward(ev, on_event)
                else:
                    on_event(ev)
                if isinstance(ev, Done):
                    full_message = ev.full_message
                if isinstance(ev, ApiError):
                    _log.info("%s_api_error model=%s", label, model)
                    return  # surface and stop

            _log.info("%s_done model=%s", label, model)

            if cancel_event.is_set():
                # If we have some content but no tool calls, we can keep it.
                # If it's empty or has orphaned tool calls, we must strip it.
                if full_message is not None:
                    # DeepSeek/OpenRouter specific: reasoning_content is NOT 'content' for the API.
                    # Standard APIs REQUIRE 'content' (string) or 'tool_calls' (list).
                    content = full_message.get("content")
                    reasoning = full_message.get("reasoning_content")

                    has_any_text = bool(content or reasoning)
                    if has_any_text:
                        full_message.pop("tool_calls", None)
                        # Normalize content to string so API doesn't reject it
                        if full_message.get("content") is None:
                            full_message["content"] = ""
                        self._history.append_assistant(full_message)
                    else:
                        self._cleanup_cancelled(on_event)
                else:
                    self._cleanup_cancelled(on_event)
                return

            if full_message is None:
                # Should not happen in normal stream completion
                return

            tool_calls = full_message.get("tool_calls") or []
            if (
                not tool_calls
                and mode in {"planner", "single"}
                and task_completion_context
            ):
                content_text = assistant_message_text(full_message)
                if final_messages_after_completion >= 1:
                    if is_repetitive_completion_final(
                        content_text,
                        last_completion_final_text,
                    ):
                        return
                    return
                self._history.append_assistant(full_message)
                final_messages_after_completion += 1
                last_completion_final_text = content_text
                return

            # Worker final quarantine: hold candidate until validation gates pass
            if not tool_calls and mode == "worker":
                candidate_final_message = full_message
            else:
                self._history.append_assistant(full_message)
                if tool_calls and stream_buffer is not None:
                    stream_buffer.flush(on_event)

            if worker_needs_final_report:
                if not tool_calls:
                    if candidate_final_message is not None:
                        self._history.append_assistant(candidate_final_message)
                        candidate_final_message = None
                    if stream_buffer is not None:
                        stream_buffer.flush(on_event)
                    return
                for tc in tool_calls:
                    fn = tc["function"]
                    name = fn["name"]
                    tool_call_id = tc["id"]
                    reason = (
                        str(worker_phase_boundary_info.get("reason"))
                        if worker_phase_boundary_info
                        else "worker_phase_boundary"
                    )
                    message = (
                        str(worker_phase_boundary_info.get("message"))
                        if worker_phase_boundary_info
                        else (
                            "Worker reached a recoverable phase boundary for this pass. "
                            "Produce the continuation report now."
                        )
                    )
                    info = {
                        "ok": False,
                        "limit_reached": bool(
                            worker_phase_boundary_info
                            and worker_phase_boundary_info.get("limit_reached")
                        ),
                        "loop_detected": bool(
                            worker_phase_boundary_info
                            and worker_phase_boundary_info.get("loop_detected")
                        ),
                        "recoverable": True,
                        "phase_boundary": True,
                        "reason": reason,
                        "tool": name,
                        "message": message,
                        "counts": limits.to_dict(),
                    }
                    self._append_limit_tool_result(tool_call_id, name, info, on_event)
                if stream_buffer is not None:
                    stream_buffer.discard()
                return

            if not tool_calls:
                if mode == "worker":
                    if has_terminal_syntax_failure(syntax_repair_required):
                        if not worker_recovery_nudge_sent:
                            diagnostic_parts = []
                            for path, state in syntax_repair_required.items():
                                if state.get("repair_failed") and state.get("error"):
                                    diagnostic_parts.append(f"{path}:\n{state['error']}")
                            diagnostic_text = "\n\n".join(diagnostic_parts)
                            instruction = (
                                "Terminal py_compile still failing after repair. "
                                "Re-read the failing Python file, fix the syntax error, "
                                "then re-run python -m py_compile. "
                                "Finish only after py_compile passes."
                            )
                            if diagnostic_text:
                                instruction += f"\n\nDiagnostic output:\n{diagnostic_text}"
                            self._history.append_user_text(instruction)
                            worker_recovery_nudge_sent = True
                            discard_worker_candidate_final()
                            continue
                        self._finish_worker_unrecoverable(
                            on_event,
                            failure_class="syntax_invalid",
                            error="Python syntax still fails after two repair attempts.",
                        )
                        return
                    # Carry import-verification paths forward for re-check
                    if import_verification_required:
                        for path in import_verification_required:
                            syntax_validation_required.add(path)
                    edit_recovery_pending = bool(
                        edit_fallback_required
                        or line_range_reread_required
                        or patch_invalid_syntax_required
                    )
                    syntax_repair_pending = bool(
                        syntax_repair_paths(syntax_repair_required)
                    )
                    if (
                        edit_recovery_pending
                        or syntax_repair_pending
                    ):
                        if not worker_recovery_nudge_sent:
                            if edit_recovery_pending:
                                if (
                                    patch_invalid_syntax_required
                                    and not edit_fallback_required
                                    and not line_range_reread_required
                                ):
                                    instruction = PATCH_CANDIDATE_INVALID_SYNTAX_ACTION
                                else:
                                    instruction = WORKER_EDIT_RECOVERY_INSTRUCTION
                            else:
                                # Distinguish craft-gate failures from terminal py_compile failures
                                any_repair_failed = any(
                                    state.get("repair_failed")
                                    for state in syntax_repair_required.values()
                                )
                                if any_repair_failed:
                                    instruction = (
                                        "Previous py_compile failed. Re-read the touched "
                                        "Python file, repair it with patch_file, then run python -m py_compile again. "
                                        "Finish only after py_compile passes."
                                    )
                                else:
                                    instruction = (
                                        "Validation caught invalid Python in the following file(s). "
                                        "Re-read the file, repair with patch_file, "
                                        "then run python -m py_compile. "
                                        "Finish only after py_compile passes."
                                    )
                            if not edit_recovery_pending:
                                diagnostic_parts = []
                                for path, state in syntax_repair_required.items():
                                    if (
                                        not state.get("awaiting_validation")
                                        and not state.get("repair_failed")
                                        and state.get("error")
                                    ):
                                        diagnostic_parts.append(f"{path}:\n{state['error']}")
                                diagnostic_text = "\n\n".join(diagnostic_parts)
                                if diagnostic_text:
                                    instruction += f"\n\nDiagnostic output:\n{diagnostic_text}"
                            self._history.append_user_text(instruction)
                            worker_recovery_nudge_sent = True
                            discard_worker_candidate_final()
                            continue
                        error_parts = [
                            "Worker stopped before recovering from a recoverable failure."
                        ]
                        details: dict[str, Any] = {}
                        if syntax_repair_pending:
                            sync_paths = sorted(
                                syntax_repair_paths(syntax_repair_required)
                            )
                            error_parts.append(
                                f" Syntax repair pending on: {', '.join(sync_paths)}."
                            )
                            details["syntax_paths"] = sync_paths
                        if edit_recovery_pending:
                            error_parts.append(
                                " Edit mechanics recovery pending."
                            )
                            details.update(edit_recovery_details(
                                edit_fallback_required,
                                line_range_reread_required,
                            ))
                            if patch_invalid_syntax_required:
                                details["patch_invalid_syntax_paths"] = sorted(
                                    patch_invalid_syntax_required
                                )
                        self._finish_worker_unrecoverable(
                            on_event,
                            failure_class="worker_recovery_exhausted",
                            error="".join(error_parts),
                            details=details or None,
                        )
                        return
                    syntax_validation_required.difference_update(
                        path for path in set(syntax_validation_required)
                        if _is_validation_scratch_path(path)
                    )
                    if syntax_validation_required:
                        product_paths = sorted(
                            _normalize_worker_path(path) for path in syntax_validation_required
                            if not _is_validation_scratch_path(path)
                        )
                        if product_paths:
                            all_ok, diagnostics = run_focused_py_compile(
                                product_paths,
                                workspace_root=self._tools.workspace_root,
                            )
                            emit_auto_py_compile_result(
                                paths=product_paths,
                                ok=all_ok,
                                diagnostics=diagnostics,
                                on_event=on_event,
                                workspace_root=self._tools.workspace_root,
                            )
                            if all_ok:
                                syntax_validation_required.clear()
                                # --- Import verification rung ---
                                import_ok, import_diag = run_focused_import_check(
                                    Path(self._tools.workspace_root),
                                    product_paths,
                                )
                                if not import_ok:
                                    for path in product_paths:
                                        import_verification_required.add(path)
                                    emit_auto_import_result(
                                        paths=product_paths,
                                        diagnostics=import_diag,
                                        on_event=on_event,
                                        workspace_root=self._tools.workspace_root,
                                    )
                                    instruction = WORKER_IMPORT_FAILURE_INSTRUCTION.format(
                                        diagnostics=import_diag,
                                    )
                                    self._history.append_user_text(instruction)
                                    discard_worker_candidate_final()
                                    continue
                                else:
                                    for path in product_paths:
                                        import_verification_required.discard(path)
                                    # --- Dependent import verification rung ---
                                    try:
                                        deps = compute_dependents(
                                            Path(self._tools.workspace_root),
                                            product_paths,
                                        )
                                        deps = deps[:15]
                                        if deps:
                                            gating_paths, gating_diag, info_diag = run_dependent_import_check(
                                                Path(self._tools.workspace_root),
                                                product_paths,
                                                deps,
                                            )
                                            if info_diag:
                                                emit_auto_dependent_import_info(
                                                    paths=deps,
                                                    diagnostics=info_diag,
                                                    on_event=on_event,
                                                    workspace_root=self._tools.workspace_root,
                                                )
                                            if gating_paths:
                                                for path in product_paths:
                                                    import_verification_required.add(path)
                                                emit_auto_import_result(
                                                    paths=gating_paths,
                                                    diagnostics=gating_diag,
                                                    on_event=on_event,
                                                    workspace_root=self._tools.workspace_root,
                                                )
                                                instruction = WORKER_DEPENDENT_CONTRACT_INSTRUCTION.format(
                                                    edited_files=", ".join(product_paths),
                                                    dependent_files=", ".join(gating_paths),
                                                    diagnostics=gating_diag,
                                                )
                                                self._history.append_user_text(instruction)
                                                discard_worker_candidate_final()
                                                continue
                                    except Exception:
                                        logging.getLogger(__name__).warning(
                                            "Dependent import check failed non-fatally",
                                            exc_info=True,
                                        )

                            else:
                                # Auto-py_compile failed — feed diagnostics back for repair
                                for path in product_paths:
                                    set_syntax_repair_state(syntax_repair_required, path, {
                                        "error": diagnostics,
                                        "failed_repairs": 0,
                                    })
                                syntax_validation_required.clear()
                                instruction = WORKER_AUTO_PY_COMPILE_INSTRUCTION.format(
                                    diagnostics=diagnostics,
                                )
                                self._history.append_user_text(instruction)
                                discard_worker_candidate_final()
                                continue
                    # --- Launch verification rung ---
                    if declared_run_command:
                        try:
                            from aura.sandbox import SandboxExecutor
                            sandbox = SandboxExecutor(
                                mode="host",
                                workspace_root=Path(self._tools.workspace_root),
                            )
                            watch = sandbox.run_and_watch(
                                declared_run_command,
                                window_seconds=10,
                            )
                            if not (watch.ok and watch.exited_early):
                                emit_auto_launch_result(
                                    command=declared_run_command,
                                    ok=False,
                                    output=watch.output,
                                    on_event=on_event,
                                    workspace_root=self._tools.workspace_root,
                                )
                                instruction = WORKER_LAUNCH_FAILURE_INSTRUCTION.format(
                                    command=declared_run_command,
                                    output=watch.output,
                                )
                                self._history.append_user_text(instruction)
                                discard_worker_candidate_final()
                                continue
                            emit_auto_launch_result(
                                command=declared_run_command,
                                ok=True,
                                output=watch.output,
                                on_event=on_event,
                                workspace_root=self._tools.workspace_root,
                            )
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "Launch verification failed non-fatally",
                                exc_info=True,
                            )
                    # --- Explicit validation commands rung ---
                    if explicit_validation_commands:
                        val_result = run_explicit_validation_commands(
                            workspace_root=Path(self._tools.workspace_root),
                            commands=explicit_validation_commands,
                        )
                        if smoothness is not None:
                            _note_validation_result(
                                smoothness,
                                gate="explicit_validation",
                                ok=val_result.ok,
                                diagnostics=val_result.diagnostics,
                            )
                        if not val_result.ok:
                            emit_explicit_validation_result(
                                command=val_result.command,
                                ok=False,
                                output=val_result.diagnostics,
                                on_event=on_event,
                                workspace_root=self._tools.workspace_root,
                            )
                            instruction = WORKER_EXPLICIT_VALIDATION_FAILURE_INSTRUCTION.format(
                                command=val_result.command,
                                diagnostics=val_result.diagnostics,
                            )
                            self._history.append_user_text(instruction)
                            discard_worker_candidate_final()
                            continue
                    # All gates passed — release candidate final and flush buffer
                    if candidate_final_message is not None:
                        if smoothness is not None:
                            _note_validation_result(smoothness, gate="final", ok=True)
                        self._history.append_assistant(candidate_final_message)
                        candidate_final_message = None
                    if stream_buffer is not None:
                        stream_buffer.flush(on_event)
                    return
                return

            _terminal_dispatch = False
            _worker_phase_boundary_info: dict[str, Any] | None = None

            # Pre-process tools sequentially to handle limits check and identify parallelizable ones
            tasks = []
            for tc in tool_calls:
                fn = tc["function"]
                name = fn["name"]
                tool_call_id = tc["id"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    err = f"failed to parse tool arguments as JSON: {exc}"
                    self._history.append_tool_result(
                        tool_call_id, json.dumps({"ok": False, "error": err})
                    )
                    on_event(
                        ToolResult(
                            tool_call_id=tool_call_id,
                            name=name,
                            ok=False,
                            result=err,
                        )
                    )
                    continue

                allowed, limit_info = limits.check(name)
                if not allowed:
                    self._append_limit_tool_result(tool_call_id, name, limit_info, on_event)
                    if is_recoverable_phase_boundary(limit_info):
                        _worker_phase_boundary_info = limit_info
                    continue
                limits.record(name)
                tasks.append({"id": tool_call_id, "name": name, "args": args})

            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            def process_task(task: dict[str, Any]) -> dict[str, Any]:
                nonlocal _terminal_dispatch, _worker_phase_boundary_info, reject_all_for_turn, worker_redispatches, stale_validation_notes
                tool_call_id = task["id"]
                name = task["name"]
                args = task["args"]

                if mode == "worker":
                    blocked = self._worker_recovery_block(
                        tool_call_id=tool_call_id,
                        name=name,
                        args=args,
                        edit_failed_shapes=edit_failed_shapes,
                        edit_fallback_required=edit_fallback_required,
                        recovery_block_counts=recovery_block_counts,
                        line_range_reread_required=line_range_reread_required,
                        worker_file_state=worker_file_state,
                        patch_failed_cycles=patch_failed_cycles,
                        patch_invalid_syntax_required=patch_invalid_syntax_required,
                        syntax_repair_required=syntax_repair_required,
                        syntax_validation_required=syntax_validation_required,
                        write_attempts_by_path=write_attempts_by_path,
                    )
                    if blocked is not None:
                        blocked_payload = parse_tool_payload(str(blocked.get("result_payload", "")))
                        if is_recoverable_phase_boundary(blocked_payload):
                            _worker_phase_boundary_info = blocked_payload
                        return blocked

                # Smoothness precheck — after recovery block which takes priority
                if mode == "worker" and smoothness is not None:
                    decision = _precheck_tool(smoothness, name=name, args=args)
                    if not decision.allowed:
                        payload = _smoothness_phase_boundary_payload(decision)
                        _worker_phase_boundary_info = payload
                        return blocked_tool_result(tool_call_id, name, payload)

                if name == "dispatch_to_worker":
                    result = self._tool_runner.handle_dispatch(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        dispatch_cb=dispatch_cb,
                    )
                    if result is not None and not result.cancelled:
                        if result.ok:
                            _terminal_dispatch = True
                        else:
                            action = classify_failed_worker_dispatch(
                                args=args,
                                result=result,
                                failures=worker_dispatch_failures,
                                failed_attempts=worker_redispatches,
                            )
                            if action["counts_as_attempt"]:
                                worker_redispatches += 1
                            blocker_reason = action["blocker_reason"]
                            if blocker_reason:
                                return {
                                    "id": tool_call_id,
                                    "blocker": True,
                                    "result": result,
                                    "blocker_reason": blocker_reason,
                                    "planner_stale_read_files": (
                                        list(result.modified_files)
                                        if result.modified_files
                                        else []
                                    ),
                                }
                    return {
                        "id": tool_call_id,
                        "skip": True,
                        "completed_dispatch_for_final": is_completed_worker_result(result),
                        "planner_stale_read_files": (
                            list(result.modified_files) if result and result.modified_files else []
                        ),
                    }

                if name == "run_research":
                    ok = self._tool_runner.handle_research(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        model=model,
                        cancel_event=cancel_event,
                        temperature=temperature,
                    )
                    if ok:
                        _terminal_dispatch = True
                    return {"id": tool_call_id, "skip": True}

                if name == "run_and_watch":
                    loop_info = self._tool_runner.handle_run_and_watch(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        cancel_event=cancel_event,
                        declared_run_command=declared_run_command or "",
                    )
                    if mode == "worker" and smoothness is not None:
                        terminal_payload = (
                            loop_info.get("_terminal_payload")
                            if isinstance(loop_info, dict)
                            else None
                        )
                        decision = _observe_tool_result(
                            smoothness,
                            name=name,
                            args=args,
                            ok=bool(
                                terminal_payload.get("ok")
                                if isinstance(terminal_payload, dict)
                                else loop_info and loop_info.get("ok")
                            ),
                            payload_text=json.dumps(loop_info) if loop_info else "",
                        )
                        if decision.phase_boundary:
                            _worker_phase_boundary_info = _smoothness_phase_boundary_payload(decision)
                    return {
                        "id": tool_call_id,
                        "skip": True,
                        "completed_tool_result_for_final": terminal_result_completed(loop_info),
                    }

                if name == "run_terminal_command":
                    loop_info = self._tool_runner.handle_terminal_command(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        cancel_event=cancel_event,
                        mode=mode,
                        explicit_validation_commands=explicit_validation_commands,
                    )
                    if mode == "worker":
                        update_syntax_state_from_terminal(
                            args=args,
                            loop_info=loop_info,
                            workspace_root=Path(self._tools.workspace_root),
                            syntax_repair_required=syntax_repair_required,
                            syntax_validation_required=syntax_validation_required,
                            stale_validation_notes=stale_validation_notes,
                        )
                    if is_recoverable_phase_boundary(loop_info):
                        _worker_phase_boundary_info = loop_info
                    # Smoothness observe for terminal commands
                    if mode == "worker" and smoothness is not None:
                        terminal_payload = (
                            loop_info.get("_terminal_payload")
                            if isinstance(loop_info, dict)
                            else None
                        )
                        payload_text = json.dumps(loop_info) if loop_info else ""
                        decision = _observe_tool_result(
                            smoothness,
                            name=name,
                            args=args,
                            ok=bool(
                                terminal_payload.get("ok")
                                if isinstance(terminal_payload, dict)
                                else loop_info and loop_info.get("ok")
                            ),
                            payload_text=payload_text,
                        )
                        if decision.phase_boundary:
                            _worker_phase_boundary_info = _smoothness_phase_boundary_payload(decision)
                    return {
                        "id": tool_call_id,
                        "skip": True,
                        "completed_tool_result_for_final": terminal_result_completed(loop_info),
                    }

                if reject_all_for_turn and name in WRITE_TOOLS:
                    payload = json.dumps(
                        {
                            "ok": False,
                            "error": "User rejected all writes in this turn.",
                            "failure_class": "approval_rejected",
                            "applied": False,
                            "write_outcome": "not_applied_user_rejected",
                        }
                    )
                    return {
                        "id": tool_call_id,
                        "result_payload": payload,
                        "event": ToolResult(
                            tool_call_id=tool_call_id,
                            name=name,
                            ok=False,
                            result=payload,
                            extras={"approval": "reject_all"},
                        )
                    }

                exec_result = self._tools.execute(
                    name=name,
                    args=args,
                    approval_cb=approval_cb,
                    reject_all=False,
                )

                # Check rejection state after execute (approval_cb could set it)
                if exec_result.extras.get("approval") == "reject_all":
                    reject_all_for_turn = True

                tool_msg_content = exec_result.to_tool_message_content()
                if mode == "worker":
                    tool_msg_content = self._update_worker_recovery_state(
                        name=name,
                        args=args,
                        ok=exec_result.ok,
                        content=tool_msg_content,
                        edit_failed_shapes=edit_failed_shapes,
                        edit_fallback_required=edit_fallback_required,
                        line_range_reread_required=line_range_reread_required,
                        worker_file_state=worker_file_state,
                        patch_failed_cycles=patch_failed_cycles,
                        patch_invalid_syntax_required=patch_invalid_syntax_required,
                        syntax_repair_required=syntax_repair_required,
                        syntax_validation_required=syntax_validation_required,
                        write_attempts_by_path=write_attempts_by_path,
                    )

                loop_result = self._apply_loop_detection(
                    mode=mode,
                    name=name,
                    args=args,
                    ok=exec_result.ok,
                    result_payload=tool_msg_content,
                )
                tool_msg_content = loop_result["content"]
                loop_info = loop_result["info"]

                # Smoothness observation — after tool result is fully processed
                if mode == "worker" and smoothness is not None:
                    decision = _observe_tool_result(
                        smoothness,
                        name=name,
                        args=args,
                        ok=exec_result.ok,
                        payload_text=tool_msg_content,
                    )
                    if decision.phase_boundary:
                        _worker_phase_boundary_info = _smoothness_phase_boundary_payload(decision)

                if is_recoverable_phase_boundary(loop_info):
                    _worker_phase_boundary_info = loop_info

                return {
                    "id": tool_call_id,
                    "result_payload": tool_msg_content,
                    "event": ToolResult(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=exec_result.ok,
                        result=tool_msg_content,
                        extras=exec_result.extras,
                    ),
                    "completed_tool_result_for_final": (
                        mode in {"planner", "single"}
                        and tool_result_completes_action(name, exec_result.ok)
                    ),
                }

            # Only parallelize read-only tools to avoid race conditions.
            read_only_tools = {"read_file", "read_file_outline", "list_directory", "grep_search", "glob"}

            # Execute tasks
            results_to_append = []

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                # We can map read tasks, and sequentialize others.
                futures = {}
                for task in tasks:
                    if cancel_event.is_set():
                        break

                    if task["name"] in read_only_tools:
                        futures[executor.submit(process_task, task)] = task
                    else:
                        # Ensure we wait for all pending reads before a write
                        for fut in concurrent.futures.as_completed(futures):
                            results_to_append.append(fut.result())
                        futures.clear()

                        if cancel_event.is_set():
                            break

                        results_to_append.append(process_task(task))

                # Wait for any remaining reads
                for fut in concurrent.futures.as_completed(futures):
                    results_to_append.append(fut.result())

            # History is not thread-safe. Reorder results by original tool_call_id order and append.
            results_by_id = {r.get("id"): r for r in results_to_append if r is not None}

            if stale_validation_notes:
                note_text = "\n".join(stale_validation_notes)
                self._history.append_user_text(note_text)

            completed_dispatch_for_final = False
            completed_tool_result_for_final = False
            planner_stale_read_files: list[str] = []
            for task in tasks:
                if cancel_event.is_set():
                    self._cleanup_cancelled(on_event)
                    return

                res = results_by_id.get(task["id"])
                if not res:
                    continue

                planner_stale_read_files.extend(
                    str(path) for path in res.get("planner_stale_read_files", [])
                )
                if res.get("blocker"):
                    self._planner_refresh.handle_post_write_notices(
                        self._history, planner_stale_read_files
                    )
                    self._append_dispatch_blocker_message(
                        res["result"], str(res.get("blocker_reason", "")), on_event
                    )
                    return
                if res.get("completed_dispatch_for_final"):
                    completed_dispatch_for_final = True
                if res.get("completed_tool_result_for_final"):
                    completed_tool_result_for_final = True
                if res.get("skip"):
                    continue

                if "result_payload" in res:
                    self._history.append_tool_result(task["id"], res["result_payload"])
                    on_event(res["event"])

            self._planner_refresh.handle_post_write_notices(
                self._history, planner_stale_read_files
            )

            if _worker_phase_boundary_info is not None:
                worker_phase_boundary_info = _worker_phase_boundary_info
                if worker_phase_boundary_info.get("smoothness_phase_boundary"):
                    self._history.append_user_text(
                        smoothness_boundary_instruction(worker_phase_boundary_info)
                    )
                elif worker_phase_boundary_info.get("message"):
                    self._history.append_user_text(str(worker_phase_boundary_info["message"]))
                worker_needs_final_report = True
                continue

            if completed_dispatch_for_final or completed_tool_result_for_final:
                task_completion_context = True
                continue

            # If research completed, stop the loop.
            # The Research Completed card is the final user-facing result.
            if _terminal_dispatch:
                return

    def _finish_worker_unrecoverable(
        self,
        on_event: EventCallback,
        *,
        failure_class: str,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "ok": False,
            "failure_class": failure_class,
            "error": error,
        }
        if details:
            payload["details"] = details
        content = json.dumps(payload, ensure_ascii=False)
        full_message = {
            "role": "assistant",
            "content": content,
            "reasoning_content": None,
        }
        self._history.append_assistant(full_message)
        on_event(ContentDelta(text=content))
        on_event(Done(finish_reason="stop", full_message=full_message))


    def _worker_recovery_block(
        self,
        *,
        tool_call_id: str,
        name: str,
        args: dict[str, Any],
        edit_failed_shapes: set[str],
        edit_fallback_required: dict[str, dict[str, Any]],
        recovery_block_counts: dict[str, int],
        line_range_reread_required: dict[str, dict[str, Any]],
        syntax_repair_required: dict[str, dict[str, Any]],
        syntax_validation_required: set[str],
        write_attempts_by_path: dict[str, int],
        worker_file_state: dict[str, dict[str, Any]] | None = None,
        patch_failed_cycles: dict[str, int] | None = None,
        patch_invalid_syntax_required: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        raw_path = _edit_shapes.tool_path(name, args)
        path = _normalize_worker_path(raw_path) if raw_path else ""
        worker_file_state = worker_file_state if worker_file_state is not None else {}
        patch_failed_cycles = patch_failed_cycles if patch_failed_cycles is not None else {}
        patch_invalid_syntax_required = (
            patch_invalid_syntax_required
            if patch_invalid_syntax_required is not None
            else {}
        )
        invalid_syntax_block = self._worker_patch_invalid_syntax_block(
            tool_call_id=tool_call_id,
            name=name,
            args=args,
            patch_invalid_syntax_required=patch_invalid_syntax_required,
            recovery_block_counts=recovery_block_counts,
        )
        if invalid_syntax_block is not None:
            return invalid_syntax_block
        if name in WORKER_RECOVERY_ALWAYS_ALLOWED:
            return None
        syntax_paths = syntax_repair_paths(syntax_repair_required)
        if syntax_paths and not syntax_repair_tool_allowed(name, args, syntax_paths):
            target = sorted(syntax_paths)[0]
            state = syntax_repair_state_for_path(syntax_repair_required, target)
            repair_failed = bool(state.get("repair_failed"))
            error_msg = (
                f"Python syntax is invalid in {target}. "
                + (
                    "Syntax still fails after one repair attempt."
                    if repair_failed
                    else "Repair that file and pass py_compile before any unrelated tool call."
                )
            )
            diagnostic = state.get("error", "")
            if diagnostic:
                error_msg += f"\n\nDiagnostic output:\n{diagnostic}"
            payload = recovery_payload(
                path=target,
                failure_class="syntax_invalid",
                error=error_msg,
                suggested_next_tool="patch_file",
                suggested_next_action=(
                    "Re-read the file, inspect proposed_context if present, then submit one corrected "
                    "patch_file transaction with the current expected_file_hash. Run py_compile after "
                    "the patch is applied."
                ),
                recoverable=not repair_failed,
            )
            record_recovery_block(payload, f"syntax:{target}:{name}", recovery_block_counts)
            return blocked_tool_result(tool_call_id, name, payload)

        if (
            path
            and name in {"edit_file", "edit_line_range"}
            and write_attempts_by_path.get(path, 0) > 3
        ):
            payload = recovery_payload(
                path=path,
                failure_class="edit_mechanics_multi_edit_spin",
                error=(
                    "Multiple failed or unapplied write attempts targeted the same file. "
                    "Use patch_file for this existing-file edit."
                ),
                suggested_next_tool="patch_file",
                suggested_next_action=(
                    "Re-read the file, then submit one patch_file containing all intended hunks."
                ),
            )
            record_recovery_block(payload, f"multi-edit-spin:{path}:{name}", recovery_block_counts)
            return blocked_tool_result(tool_call_id, name, payload)

        if name == "apply_edit_transaction" and path:
            shape = _edit_shapes.edit_shape_signature(name, args)
            if shape in edit_failed_shapes:
                if f"ambiguous-replace-text:{shape}" in edit_failed_shapes:
                    payload = recovery_payload(
                        path=path,
                        failure_class="edit_transaction_ambiguous_symbol",
                        error=(
                            "Repeated ambiguous replace_text_once transaction. "
                            "Do not retry the same exact text shape."
                        ),
                        suggested_next_tool="patch_file",
                        suggested_next_action=(
                            "Re-read the file, then retry patch_file with occurrence or allow_multiple."
                        ),
                        recoverable=False,
                    )
                    record_recovery_block(payload, shape, recovery_block_counts)
                    return blocked_tool_result(tool_call_id, name, payload)
                payload = recovery_payload(
                    path=path,
                    failure_class="edit_mechanics_blocked",
                    error="Repeated apply_edit_transaction failure. Re-read the file and return a concise blocker instead of switching between edit tools.",
                    suggested_next_tool="read_file",
                    suggested_next_action="Re-read the file, then report the typed transaction blocker if the structured operation still cannot be applied safely.",
                    recoverable=False,
                )
                record_recovery_block(payload, shape, recovery_block_counts)
                return blocked_tool_result(tool_call_id, name, payload)

        if name == "edit_line_range" and path in line_range_reread_required:
            payload = recovery_payload(
                path=path,
                failure_class="edit_mechanics_stale_line_range",
                error="Stale line range after a failed edit_line_range. Re-read the file before retrying line-range editing.",
                suggested_next_tool="read_file",
                suggested_next_action="Re-read the file before retrying an edit.",
            )
            record_recovery_block(payload, f"line-range-reread:{path}", recovery_block_counts)
            return blocked_tool_result(tool_call_id, name, payload)

        if name == "patch_file" and path:
            blocked = self._worker_patch_file_state_block(
                tool_call_id=tool_call_id,
                name=name,
                path=path,
                args=args,
                worker_file_state=worker_file_state,
            )
            if blocked is not None:
                return blocked
            shape = _edit_shapes.edit_shape_signature(name, args)
            failed_cycles = patch_failed_cycles.get(shape, 0)
            if failed_cycles >= 1:
                payload = recovery_payload(
                    path=path,
                    failure_class="patch_file_repeated_failure",
                    error=(
                        "Repeated patch shape failed. This exact patch_file shape is blocked; "
                        "a different hunk shape for the same file is still valid."
                    ),
                    suggested_next_tool="patch_file",
                    suggested_next_action=(
                        "Change the patch shape by adding occurrence, using more surrounding context, "
                        "changing the old block, or using a fresh expected_file_hash from a new read."
                    ),
                    recoverable=False,
                )
                payload["applied"] = False
                payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
                payload["patch_failed_cycles"] = failed_cycles
                payload["patch_shape"] = _edit_shapes.shape_digest(shape)
                record_recovery_block(payload, f"patch-shape:{shape}", recovery_block_counts)
                return blocked_tool_result(tool_call_id, name, payload)

        if name in ("edit_file", "edit_symbol") and path in edit_fallback_required:
            prior = edit_fallback_required[path]
            block_key = _edit_shapes.edit_shape_signature(name, args)
            payload = recovery_payload(
                path=path,
                failure_class=str(prior.get("failure_class") or default_edit_failure_class(name)),
                error="Repeated failed edit tactic. Do not retry this edit shape. Re-read the file and use patch_file for existing-file code changes.",
                suggested_next_tool="patch_file",
                suggested_next_action="Use read_file/read_file_outline, then submit one patch_file with exact hunks.",
            )
            payload["previous_error"] = prior.get("error", "")
            record_recovery_block(payload, block_key, recovery_block_counts)
            return blocked_tool_result(tool_call_id, name, payload)

        if name in ("edit_file", "edit_symbol", "edit_line_range"):
            shape = _edit_shapes.edit_shape_signature(name, args)
            if shape in edit_failed_shapes:
                payload = recovery_payload(
                    path=path,
                    failure_class=default_edit_failure_class(name),
                    error="Repeated failed edit tactic. Do not retry this edit shape. Re-read the file and use patch_file for existing-file code changes.",
                    suggested_next_tool="patch_file",
                    suggested_next_action="Use read_file/read_file_outline, then submit one patch_file with exact hunks.",
                )
                record_recovery_block(payload, shape, recovery_block_counts)
                return blocked_tool_result(tool_call_id, name, payload)

        return None

    def _worker_patch_invalid_syntax_block(
        self,
        *,
        tool_call_id: str,
        name: str,
        args: dict[str, Any],
        patch_invalid_syntax_required: dict[str, dict[str, Any]],
        recovery_block_counts: dict[str, int],
    ) -> dict[str, Any] | None:
        if not patch_invalid_syntax_required:
            return None

        target_path = _pending_patch_invalid_syntax_path(
            name,
            args,
            patch_invalid_syntax_required,
        )
        pending_path = target_path or sorted(patch_invalid_syntax_required)[0]
        state = patch_invalid_syntax_required.get(pending_path) or {}
        if state.get("retry_failed") or state.get("blocked"):
            payload = recovery_payload(
                path=pending_path,
                failure_class=PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS,
                error=(
                    "patch_file candidate syntax recovery already used its one retry. "
                    "Stop and return a concise blocker."
                ),
                suggested_next_tool="none",
                suggested_next_action="Stop and summarize the blocker; do not call more tools for this patch.",
                recoverable=False,
            )
            payload["applied"] = False
            payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
            payload["patch_shape"] = state.get("patch_shape")
            record_recovery_block(
                payload,
                f"patch-invalid-syntax-blocked:{pending_path}",
                recovery_block_counts,
            )
            return blocked_tool_result(tool_call_id, name, payload)

        if target_path and name in {"read_file", "read_file_range", "read_files"}:
            return None

        if not state.get("reread_done"):
            payload = recovery_payload(
                path=pending_path,
                failure_class=PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS,
                error="patch_file candidate syntax recovery requires re-reading the suggested range before any other tool.",
                suggested_next_tool="read_file_range",
                suggested_next_action=PATCH_CANDIDATE_INVALID_SYNTAX_ACTION,
            )
            payload["applied"] = False
            payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
            payload["patch_shape"] = state.get("patch_shape")
            record_recovery_block(
                payload,
                f"patch-invalid-syntax-reread:{pending_path}:{name}",
                recovery_block_counts,
            )
            return blocked_tool_result(tool_call_id, name, payload)

        if name == "patch_file" and target_path:
            return None

        payload = recovery_payload(
            path=pending_path,
            failure_class=PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS,
            error="Target area was re-read. Retry patch_file once now; do not use unrelated tools.",
            suggested_next_tool="patch_file",
            suggested_next_action=PATCH_CANDIDATE_INVALID_SYNTAX_ACTION,
        )
        payload["applied"] = False
        payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
        payload["patch_shape"] = state.get("patch_shape")
        record_recovery_block(
            payload,
            f"patch-invalid-syntax-retry:{pending_path}:{name}",
            recovery_block_counts,
        )
        return blocked_tool_result(tool_call_id, name, payload)

    def _worker_patch_file_state_block(
        self,
        *,
        tool_call_id: str,
        name: str,
        path: str,
        args: dict[str, Any],
        worker_file_state: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not worker_path_is_existing_file(self._tools.workspace_root, path):
            return None

        expected_hash = args.get("expected_file_hash")
        if not isinstance(expected_hash, str) or not expected_hash:
            payload = recovery_payload(
                path=path,
                failure_class="patch_file_missing_expected_hash",
                error=(
                    "Worker patch_file on an existing file requires expected_file_hash "
                    "from the latest successful read."
                ),
                suggested_next_tool="read_file",
                suggested_next_action=(
                    "Read the file, then retry patch_file with expected_file_hash "
                    "set to the returned content_hash."
                ),
            )
            payload["applied"] = False
            payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
            return blocked_tool_result(tool_call_id, name, payload)

        state = worker_file_state_for_path(worker_file_state, path)
        known_hash = str(state.get("content_hash") or "") if state else ""
        if not state or known_hash != expected_hash or not state.get("fresh_for_patch"):
            payload = recovery_payload(
                path=path,
                failure_class="patch_file_hash_mismatch",
                error=(
                    "patch_file expected_file_hash does not match the Worker's "
                    "latest successful read for this file."
                ),
                suggested_next_tool="read_file",
                suggested_next_action=(
                    "Re-read the file with read_file or read_file_range, then retry patch_file once "
                    "with expected_file_hash set to the new content_hash."
                ),
            )
            payload["applied"] = False
            payload["write_outcome"] = "not_applied_edit_mechanics_blocked"
            payload["recoverable"] = True
            payload["stale"] = True
            payload["expected_file_hash"] = expected_hash
            if known_hash:
                payload["latest_read_content_hash"] = known_hash
            if state and state.get("last_read_tool"):
                payload["last_read_tool"] = state.get("last_read_tool")
            return blocked_tool_result(tool_call_id, name, payload)

        return None

    def _update_worker_recovery_state(
        self,
        *,
        name: str,
        args: dict[str, Any],
        ok: bool,
        content: str,
        edit_failed_shapes: set[str],
        edit_fallback_required: dict[str, dict[str, Any]],
        line_range_reread_required: dict[str, dict[str, Any]],
        syntax_repair_required: dict[str, dict[str, Any]],
        syntax_validation_required: set[str],
        write_attempts_by_path: dict[str, int],
        worker_file_state: dict[str, dict[str, Any]] | None = None,
        patch_failed_cycles: dict[str, int] | None = None,
        patch_invalid_syntax_required: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        worker_file_state = worker_file_state if worker_file_state is not None else {}
        patch_failed_cycles = patch_failed_cycles if patch_failed_cycles is not None else {}
        patch_invalid_syntax_required = (
            patch_invalid_syntax_required
            if patch_invalid_syntax_required is not None
            else {}
        )
        parsed = parse_tool_payload(content)
        record_reads_for_recovery(
            name,
            args,
            parsed,
            line_range_reread_required,
            edit_fallback_required,
            worker_file_state,
        )
        raw_path = _edit_shapes.tool_path(name, args, parsed)
        path = _normalize_worker_path(raw_path) if raw_path else ""
        _mark_patch_invalid_syntax_reread(
            name,
            args,
            parsed,
            patch_invalid_syntax_required,
        )
        if name in WRITE_TOOLS and path and (
            not ok
            or (
                isinstance(parsed, dict)
                and parsed.get("ok")
                and parsed.get("applied") is False
            )
        ):
            write_attempts_by_path[path] = write_attempts_by_path.get(path, 0) + 1

        if ok:
            if name in WRITE_TOOLS and path:
                is_deletion = (
                    name == "delete_file"
                    or (isinstance(parsed, dict) and parsed.get("deleted") is True)
                )
                if is_deletion:
                    pop_normalized_recovery_key(edit_fallback_required, path)
                    pop_normalized_recovery_key(line_range_reread_required, path)
                    pop_normalized_key(worker_file_state, path)
                    clear_patch_failed_shapes_for_path(patch_failed_cycles, path, _edit_shapes.parse_patch_shape)
                    pop_normalized_recovery_key(patch_invalid_syntax_required, path)
                    pop_syntax_repair_state(syntax_repair_required, path)
                    discard_syntax_validation_path(syntax_validation_required, path)
                    return content

                pop_normalized_recovery_key(edit_fallback_required, path)
                pop_normalized_recovery_key(line_range_reread_required, path)
                pop_normalized_key(worker_file_state, path)
                clear_patch_failed_shapes_for_path(patch_failed_cycles, path, _edit_shapes.parse_patch_shape)
                pop_normalized_recovery_key(patch_invalid_syntax_required, path)
                if is_python_path(path) and not _is_validation_scratch_path(path):
                    syntax_validation_required.add(path)
                state = syntax_repair_state_for_path(syntax_repair_required, path)
                if state:
                    state["repair_attempted"] = True
                    state["awaiting_validation"] = True
                    set_syntax_repair_state(syntax_repair_required, path, state)
                    if not _is_validation_scratch_path(path):
                        syntax_validation_required.add(path)
            return content

        if name in ("edit_file", "edit_symbol", "edit_line_range", "apply_edit_transaction"):
            edit_failed_shapes.add(_edit_shapes.edit_shape_signature(name, args))

        if not isinstance(parsed, dict):
            return content

        failure_class = str(parsed.get("failure_class", ""))
        shape = _edit_shapes.edit_shape_signature(name, args)
        prior_invalid_syntax = (
            normalized_state_value(patch_invalid_syntax_required, path)
            if path and name == "patch_file"
            else None
        )
        if (
            path
            and name == "patch_file"
            and isinstance(prior_invalid_syntax, dict)
            and prior_invalid_syntax.get("reread_done")
            and failure_class
        ):
            prior_invalid_syntax["retry_failed"] = True
            prior_invalid_syntax["blocked"] = True
            blocked_state = {
                **prior_invalid_syntax,
                "retry_failed": True,
                "blocked": True,
            }
            patch_invalid_syntax_required[_normalize_worker_path(path)] = blocked_state
            parsed.setdefault("applied", False)
            parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
            parsed["recoverable"] = False
            parsed["failure_class"] = (
                PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS
                if failure_class == PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS
                else PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS
            )
            parsed["error"] = (
                "patch_file retry failed after candidate syntax recovery. "
                "Stop and return a concise blocker. "
                + str(parsed.get("error", ""))
            ).strip()
            parsed["suggested_next_tool"] = "none"
            parsed["suggested_next_action"] = (
                "Stop and summarize the blocker; do not call more tools for this patch."
            )
            parsed["patch_shape"] = prior_invalid_syntax.get("patch_shape")
            return json.dumps(parsed, ensure_ascii=False)

        if path and failure_class in EDIT_TRANSACTION_FAILURE_CLASSES:
            parsed.setdefault("applied", False)
            parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
            parsed["recoverable"] = False
            parsed.pop("suggested_tool", None)
            parsed.pop("suggested_next_tool", None)
            if (
                failure_class == "edit_transaction_ambiguous_symbol"
                and _edit_shapes.has_replace_text_once_operation(args)
            ):
                edit_failed_shapes.add(f"ambiguous-replace-text:{shape}")
                parsed["suggested_next_action"] = (
                    "Re-read the file, then retry patch_file with occurrence "
                    "or allow_multiple."
                )
            else:
                parsed["suggested_next_action"] = (
                    "Transaction could not be applied safely. Re-read the file "
                    "and report this typed blocker if the operation is still not applicable."
                )
            content = json.dumps(parsed, ensure_ascii=False)
        elif path and name != "patch_file" and failure_class in EDIT_MECHANICS_FAILURE_CLASSES:
            parsed.setdefault("applied", False)
            parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
            parsed.setdefault("tool", name)
            edit_fallback_required[path] = parsed
            parsed["recoverable"] = True
            parsed["suggested_next_tool"] = "patch_file"
            parsed["suggested_next_action"] = "Do not retry this low-level edit shape. Re-read the file and submit one patch_file for existing-file code changes."
            content = json.dumps(parsed, ensure_ascii=False)
        elif path and failure_class == "edit_mechanics_stale_line_range":
            parsed.setdefault("applied", False)
            parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
            parsed.setdefault("tool", name)
            line_range_reread_required[path] = parsed
            parsed["recoverable"] = True
            parsed["suggested_next_tool"] = "read_file"
            parsed["suggested_next_action"] = "Re-read the file before retrying an edit."
            content = json.dumps(parsed, ensure_ascii=False)
        elif path and name == "patch_file" and failure_class == PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS:
            parsed.setdefault("applied", False)
            parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
            parsed.setdefault("tool", name)
            patch_shape = _edit_shapes.edit_shape_signature(name, args)
            failed_cycles = patch_failed_cycles.get(patch_shape, 0) + 1
            patch_failed_cycles[patch_shape] = failed_cycles
            parsed["patch_failed_cycles"] = failed_cycles
            parsed["patch_shape"] = _edit_shapes.shape_digest(patch_shape)
            parsed["suggested_next_tool"] = "read_file_range"
            parsed["suggested_next_action"] = PATCH_CANDIDATE_INVALID_SYNTAX_ACTION
            if failed_cycles >= 2:
                parsed["recoverable"] = False
                parsed["failure_class"] = PATCH_CANDIDATE_INVALID_SYNTAX_REPEATED_CLASS
                parsed["error"] = (
                    "Repeated patch_file candidate syntax failure for the same patch shape. "
                    "Stop and return a concise blocker."
                )
                pop_normalized_recovery_key(patch_invalid_syntax_required, path)
            else:
                patch_invalid_syntax_required[_normalize_worker_path(path)] = {
                    "failure_class": PATCH_CANDIDATE_INVALID_SYNTAX_FAILURE_CLASS,
                    "error": parsed.get("error", ""),
                    "shape": patch_shape,
                    "patch_shape": parsed["patch_shape"],
                    "reread_done": False,
                    "retry_failed": False,
                }
                parsed["recoverable"] = True
            content = json.dumps(parsed, ensure_ascii=False)
        elif path and name == "patch_file" and failure_class in {"patch_hunk_not_found", "patch_hunk_ambiguous", "patch_file_hash_mismatch"}:
            parsed.setdefault("applied", False)
            parsed.setdefault("write_outcome", "not_applied_edit_mechanics_blocked")
            parsed.setdefault("tool", name)
            patch_shape = _edit_shapes.edit_shape_signature(name, args)
            failed_cycles = patch_failed_cycles.get(patch_shape, 0) + 1
            patch_failed_cycles[patch_shape] = failed_cycles
            parsed["patch_failed_cycles"] = failed_cycles
            parsed["patch_shape"] = _edit_shapes.shape_digest(patch_shape)
            parsed["stale"] = True
            parsed["suggested_next_tool"] = "read_file"
            if failure_class == "patch_file_hash_mismatch":
                parsed["suggested_next_action"] = (
                    "Re-read the file with read_file or read_file_range, then retry patch_file once "
                    "with expected_file_hash set to the returned content_hash."
                )
            else:
                parsed["suggested_next_action"] = (
                    "Re-read the file, then retry patch_file once with current exact text "
                    "and expected_file_hash set to the returned content_hash."
                )
            if failed_cycles >= 2:
                parsed["recoverable"] = False
                parsed["failure_class"] = "patch_file_repeated_failure"
                parsed["error"] = (
                    "Repeated patch shape failed. This exact patch_file shape is blocked; "
                    "a different hunk shape for the same file is still valid."
                )
                pop_normalized_recovery_key(edit_fallback_required, path)
            else:
                edit_fallback_required[path] = parsed
                parsed["recoverable"] = True
            content = json.dumps(parsed, ensure_ascii=False)
        elif path and failure_class == "syntax_invalid":
            parsed.setdefault("applied", False)
            parsed.setdefault("write_outcome", "not_applied_craft_rejected")
            state = syntax_repair_state_for_path(syntax_repair_required, path)
            if not state:
                state = {"failed_repairs": 0}
            set_syntax_repair_state(syntax_repair_required, path, state)
            state["awaiting_validation"] = False
            if name in WRITE_TOOLS:
                state["failed_repairs"] = int(state.get("failed_repairs", 0)) + 1
            parsed["suggested_next_tool"] = "patch_file"
            parsed["suggested_next_action"] = (
                "Re-read the file, inspect proposed_context if present, then submit one corrected "
                "patch_file transaction with the current expected_file_hash. Run py_compile after "
                "the patch is applied."
            )
            if int(state.get("failed_repairs", 0)) > 1:
                parsed["recoverable"] = False
                parsed["error"] = "Syntax repair failed after one repair attempt. " + str(parsed.get("error", ""))
            content = json.dumps(parsed, ensure_ascii=False)

        return content

    def _append_limit_tool_result(
        self,
        tool_call_id: str,
        name: str,
        info: dict[str, Any],
        on_event: EventCallback,
    ) -> None:
        payload = limit_reached_payload(info)
        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                ok=False,
                result=payload,
                extras={
                    "limit_reached": bool(info.get("limit_reached")),
                    "recoverable": bool(info.get("recoverable")),
                    "phase_boundary": bool(info.get("phase_boundary")),
                    "reason": str(info.get("reason", "")),
                },
            )
        )

    def _append_dispatch_blocker_message(
        self,
        result: WorkerDispatchResult,
        reason: str,
        on_event: EventCallback,
    ) -> None:
        if reason == "internal":
            message = (
                "Harness error due to an internal Worker exception. "
                "I stopped automatic redispatch to avoid repeating the same handoff."
            )
        elif reason == "repeated":
            if result.extras.get("dispatch_spec_rejected"):
                message = (
                    "Plan incomplete — missing required dispatch details. "
                    "The same Worker handoff was rejected twice, so I stopped automatic redispatch."
                )
            elif result.mismatch is not None or result.extras.get("planner_resolution_needed"):
                message = (
                    "The same Worker mismatch was returned twice. I stopped automatic redispatch "
                    "so the Planner handoff can change."
                )
            else:
                message = (
                    "The same Worker dispatch failed twice with the same result. "
                    "I stopped automatic redispatch so the plan can be corrected first."
                )
        elif reason == "limit":
            message = (
                "Worker dispatch did not complete after "
                f"{MAX_WORKER_REDISPATCHES_PER_USER_TURN} failed attempts this turn. "
                "I stopped automatic redispatch so the next handoff can change."
            )
        else:
            message = (
                "Harness error. I stopped automatic redispatch so the failure can be addressed first."
            )
        on_event(ContentDelta(text=message))
        full_message = {
            "role": "assistant",
            "content": message,
            "reasoning_content": None,
        }
        self._history.append_assistant(full_message)
        on_event(Done(finish_reason="stop", full_message=full_message))

    def _apply_loop_detection(
        self,
        *,
        mode: str,
        name: str,
        args: dict[str, Any],
        ok: bool,
        result_payload: str,
    ) -> dict[str, Any]:
        """Track repetitive failures and return annotated content plus metadata."""
        observed = self._loop_detector.observe(
            mode=mode,
            tool_name=name,
            args=args,
            ok=ok,
            content=result_payload,
        )
        return {"content": observed.content, "info": observed.info}

    def _cleanup_cancelled(self, on_event: EventCallback) -> None:
        """Call this when a turn is cancelled while waiting for model or tool.
        Ensure history doesn't contain an assistant message with pending tool calls
        that haven't been followed by tool result messages.
        """
        if not self._history.messages:
            on_event(ApiError(status_code=None, message="Cancelled."))
            return

        # We look for the MOST RECENT assistant message.
        # If it has tool calls that are missing results, we MUST clean it up.
        for i in range(len(self._history.messages) - 1, -1, -1):
            msg = self._history.messages[i]
            if msg.get("role") == "user":
                # If we hit a user message first, it means the turn was cancelled
                # before the assistant even started responding.
                break

            if msg.get("role") == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    call_ids = {tc["id"] for tc in tool_calls}
                    # Look at messages following this one.
                    for j in range(i + 1, len(self._history.messages)):
                        m = self._history.messages[j]
                        if m.get("role") == "tool":
                            call_ids.discard(m.get("tool_call_id"))

                    if call_ids:
                        # Incomplete! Truncate history back to BEFORE this assistant message.
                        # We find the user message that preceded it.
                        user_idx = -1
                        for k in range(i - 1, -1, -1):
                            if self._history.messages[k].get("role") == "user":
                                user_idx = k
                                break
                        if user_idx != -1:
                            self._history.truncate_after(user_idx + 1)
                        else:
                            self._history.truncate_after(i)
                elif not msg.get("content") and not msg.get("reasoning_content"):
                    # Empty assistant message — strip it.
                    self._history.truncate_after(i)
                break

        on_event(ApiError(status_code=None, message="Cancelled."))


def _pending_patch_invalid_syntax_path(
    name: str,
    args: dict[str, Any],
    pending: dict[str, dict[str, Any]],
) -> str:
    normalized_pending = {_normalize_worker_path(path): path for path in pending}
    if name == "read_files":
        paths = args.get("paths")
        if isinstance(paths, list):
            for raw in paths:
                normalized = _normalize_worker_path(str(raw))
                if normalized in normalized_pending:
                    return normalized_pending[normalized]
        return ""

    raw_path = _edit_shapes.tool_path(name, args)
    if not raw_path:
        return ""
    normalized = _normalize_worker_path(raw_path)
    return normalized_pending.get(normalized, "")


def _mark_patch_invalid_syntax_reread(
    name: str,
    args: dict[str, Any],
    parsed: Any,
    pending: dict[str, dict[str, Any]],
) -> None:
    if name not in {"read_file", "read_file_range", "read_files"}:
        return
    if not isinstance(parsed, dict):
        return

    def mark_one(path: str, result: dict[str, Any]) -> None:
        if result.get("ok") is not True:
            return
        if result.get("truncated") is True:
            return
        content_hash = result.get("content_hash")
        file_size = result.get("file_size")
        if not isinstance(content_hash, str) or not content_hash:
            return
        if not isinstance(file_size, int):
            return
        normalized = _normalize_worker_path(path)
        state = normalized_state_value(pending, normalized)
        if not isinstance(state, dict):
            return
        state["reread_done"] = True
        state["latest_read_content_hash"] = content_hash
        state["last_read_tool"] = name
        pending[normalized] = state

    if name == "read_files":
        files = parsed.get("files")
        if not isinstance(files, dict):
            return
        for path_key, result in files.items():
            if isinstance(result, dict):
                mark_one(str(result.get("path") or path_key), result)
        return

    path = _edit_shapes.tool_path(name, args, parsed)
    if path:
        mark_one(path, parsed)


__all__ = [
    "ConversationManager",
    "ApprovalCallback",
    "ApprovalDecision",
    "ApprovalRequest",
    "EventCallback",
    "Event",
    "ReasoningDelta",
    "ContentDelta",
    "ToolCallStart",
    "ToolCallArgsDelta",
    "ToolCallEnd",
    "Usage",
    "Done",
    "ApiError",
    "ToolResult",
    "WorkerDispatchRequested",
    "TerminalOutput",
    "DispatchCallback",
    "WorkerDispatchRequest",
    "WorkerDispatchResult",
]
