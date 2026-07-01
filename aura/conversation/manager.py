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
import re
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
from aura.conversation.completion_guard import (
    assistant_message_text,
    is_repetitive_completion_final,
    terminal_result_completed,
    tool_result_completes_action,
    worker_dispatch_is_terminal,
)
from aura.conversation.critic_dispatch import CriticCallback
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.dispatch_failure import classify_failed_worker_dispatch
from aura.conversation.edit_orchestrator import (
    EditRetryLedger,
)
from aura.conversation.edit_recovery_state import (
    edit_recovery_details,
)
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.manager_recovery import (
    update_worker_recovery_state,
    worker_recovery_block,
)
from aura.conversation.manager_send_state import _SendState
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.path_utils import (
    normalize_worker_path as _normalize_worker_path,
)
from aura.conversation.planner_stream_hygiene import PlannerStreamHygiene
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.syntax_repair_state import (
    has_terminal_syntax_failure,
    set_syntax_repair_state,
    syntax_repair_paths,
)
from aura.conversation.syntax_terminal_state import update_syntax_state_from_terminal
from aura.conversation.tool_limits import (
    WRITE_TOOLS,
    limit_reached_payload,
)
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.verification_progress import VerificationProgressTracker
from aura.conversation.worker_final_validation import (
    WORKER_EXPLICIT_VALIDATION_FAILURE_INSTRUCTION,
    emit_explicit_validation_result,
    emit_explicit_validation_runs,
    run_explicit_validation_commands,
)
from aura.conversation.worker_fingerprints import fingerprint_paths
from aura.conversation.worker_finish import (
    build_worker_recoverable_followup_message,
    build_worker_unrecoverable_message,
)
from aura.conversation.worker_flow import (
    WORKER_FLOW_VALIDATION_REQUIRED_TEXT,
    WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT,
)
from aura.conversation.worker_quality_gate import handle_worker_quality_gate
from aura.conversation.worker_recovery_messages import (
    PATCH_CANDIDATE_INVALID_SYNTAX_ACTION,
    WORKER_AUTO_PY_COMPILE_INSTRUCTION,
    WORKER_DEPENDENT_CONTRACT_INSTRUCTION,
    WORKER_EDIT_RECOVERY_INSTRUCTION,
    WORKER_IMPORT_FAILURE_INSTRUCTION,
    WORKER_LAUNCH_FAILURE_INSTRUCTION,
)
from aura.conversation.worker_recovery_payload import (
    blocked_tool_result,
    is_recoverable_phase_boundary,
    parse_tool_payload,
)
from aura.conversation.worker_validation import (
    emit_auto_dependent_import_info,
    emit_auto_import_result,
    emit_auto_launch_result,
    emit_auto_py_compile_result,
    run_focused_py_compile,
)
from aura.dependency_context import compute_dependents
from aura.hooks import hooks
from aura.research.policy import ANSWER_ONLY, decide_research_policy
from aura.verify import run_dependent_import_check, run_focused_import_check

EventCallback = Callable[[Event], None]

WORKER_FINAL_REPORT_PROOF_REQUIRED_TEXT = (
    "Worker final report is missing explicit validation or acceptance proof. "
    "Reply with the final report only and include concrete lines for changed files, "
    "validation command/result, and acceptance verification."
)

_LOCAL_CODE_INTENT_RE = re.compile(
    r"\b(?:fix|add|update|change|modify|edit|patch|refactor|extract|move|"
    r"create|remove|delete|rename|implement|test|py_compile|pytest|import|"
    r"module|function|class|file)\b",
    re.IGNORECASE,
)

_FINAL_REPORT_INCOMPLETE_PROOF_RE = re.compile(
    r"\b(?:not\s+(?:tested|validated|verified)|validation\s+(?:did\s+not|didn't|"
    r"not)\s+run|failed\s+(?:validation|acceptance)|(?:validation|acceptance)\s+failed|"
    r"could\s+not\s+(?:verify|run)|couldn't\s+(?:verify|run)|unable\s+to\s+(?:verify|run))\b",
    re.IGNORECASE,
)

_FINAL_REPORT_VALIDATION_PROOF_RE = re.compile(
    r"\b(?:verified|validated|pytest|py_compile|ruff|mypy|tests?\s+pass(?:ed|es)?|"
    r"compiled|exit\s+code\s+0|exits\s+0)\b",
    re.IGNORECASE,
)

_FINAL_REPORT_ACCEPTANCE_PROOF_RE = re.compile(
    r"\b(?:acceptance|accepted)\b.{0,80}\b(?:verified|validated|passed|met|satisfied|confirmed|ok)\b|"
    r"\b(?:verified|validated|passed|met|satisfied|confirmed)\b.{0,80}\bacceptance\b",
    re.IGNORECASE | re.DOTALL,
)

_ALLOWED_ZERO_WORK_FAILURE_CLASSES = frozenset(
    {
        "approval_rejected",
        "cancelled",
        "conflicting_spec",
        "dispatch_blocked",
        "dispatch_not_started",
        "external_validation_runtime_missing",
        "file_not_found",
        "impossible_spec",
        "missing_file",
        "missing_path",
        "missing_required_file",
        "path_not_found",
        "permission_denied",
        "required_path_missing",
        "runtime_environment_missing",
        "source_inspection_command_blocked",
        "tool_failure",
        "tool_permission_denied",
        "user_cancelled",
        "validation_environment_missing",
        "write_rejected",
    }
)

_ALLOWED_ZERO_WORK_FAILURE_PREFIXES = (
    "project_environment_missing_",
    "permission_",
)

_ALLOWED_ZERO_WORK_BLOCKER_RE = re.compile(
    r"\b(?:required\s+)?(?:file|path|directory)\b.{0,80}\b"
    r"(?:missing|not\s+found|does\s+not\s+exist|unavailable)\b|"
    r"\b(?:permission|access)\s+denied\b|"
    r"\b(?:cannot|can't|could\s+not|couldn't|unable\s+to)\s+(?:read|write|access)\b|"
    r"\b(?:missing|unavailable)\s+(?:runtime|environment|tool|dependency|executable)\b|"
    r"\b(?:conflicting|impossible)\s+(?:spec|requirements?)\b|"
    r"\bneeds_planner_resolution\b",
    re.IGNORECASE | re.DOTALL,
)




def _terminal_payload(loop_info: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(loop_info, dict):
        return {}
    payload = loop_info.get("_terminal_payload")
    return payload if isinstance(payload, dict) else {}


def _terminal_payload_ok(loop_info: dict[str, Any] | None) -> bool | None:
    payload = _terminal_payload(loop_info)
    if "ok" not in payload:
        return None
    return bool(payload.get("ok"))


def _worker_final_report_claims_validation_or_acceptance(content: str) -> bool:
    text = str(content or "")
    if _FINAL_REPORT_INCOMPLETE_PROOF_RE.search(text):
        return False
    return bool(
        _FINAL_REPORT_VALIDATION_PROOF_RE.search(text)
        or _FINAL_REPORT_ACCEPTANCE_PROOF_RE.search(text)
    )


def _worker_final_report_needs_proof(state: _SendState) -> bool:
    flow = state.worker_flow
    if flow is None:
        return False
    return int(getattr(flow.state, "write_actions", 0) or 0) > 0


def _worker_final_report_missing_proof(state: _SendState, full_message: dict[str, Any]) -> bool:
    if state.worker_final_report_proof_nudge_sent:
        return False
    if not _worker_final_report_needs_proof(state):
        return False
    return not _worker_final_report_claims_validation_or_acceptance(
        assistant_message_text(full_message)
    )


def _worker_has_zero_applied_writes(state: _SendState) -> bool:
    flow = state.worker_flow
    write_actions = int(getattr(flow.state, "write_actions", 0) or 0) if flow else 0
    return write_actions == 0 and not state.worker_app_writes


def _worker_has_attempted_write(state: _SendState) -> bool:
    flow = state.worker_flow
    write_intents = int(getattr(flow.state, "write_intents", 0) or 0) if flow else 0
    return write_intents > 0 or bool(state.write_attempts_by_path)


def _candidate_final_payload(full_message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(full_message, dict):
        return {}
    try:
        parsed = json.loads(assistant_message_text(full_message))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate_final_has_real_zero_work_blocker(
    full_message: dict[str, Any] | None,
) -> bool:
    payload = _candidate_final_payload(full_message)
    if payload.get("status") == "needs_planner_resolution":
        return bool(
            payload.get("mismatch")
            or payload.get("question")
            or payload.get("question_for_planner")
            or payload.get("error")
        )
    failure_class = str(payload.get("failure_class") or "")
    if failure_class in _ALLOWED_ZERO_WORK_FAILURE_CLASSES:
        return True
    if any(failure_class.startswith(prefix) for prefix in _ALLOWED_ZERO_WORK_FAILURE_PREFIXES):
        return True
    if payload.get("reject") or payload.get("dispatch_not_started"):
        return True
    return bool(_ALLOWED_ZERO_WORK_BLOCKER_RE.search(assistant_message_text(full_message or {})))


class ConversationManager:
    def __init__(
        self,
        history: History,
        tool_registry: ToolRegistry,
    ) -> None:
        self._history = history
        self._tools = tool_registry
        self._loop_detector = LoopDetector()
        self._verification_tracker = VerificationProgressTracker()
        self._tool_runner = ToolRunner(
            history=self._history,
            workspace_root=self._tools.workspace_root,
            loop_detector=self._loop_detector,
            verification_tracker=self._verification_tracker,
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
        critic_cb: CriticCallback | None = None,
        worker_dispatch_request: WorkerDispatchRequest | None = None,
        dispatch_tool_call_id: str = "",
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

        mode = getattr(self._tools, "mode", "single")
        state = _SendState(
            mode=mode,
            research_policy=decide_research_policy(_latest_user_text(self._history)),
        )

        while True:
            if (
                state.mode in {"planner", "single"}
                and state.task_completion_context
                and state.final_messages_after_completion >= 1
            ):
                return

            state.rounds_used += 1
            if max_tool_rounds is not None and state.rounds_used > max_tool_rounds:
                on_event(ApiError(status_code=None, message=f"Exceeded max tool rounds ({max_tool_rounds})."))
                return

            state.limits.begin_model_round()
            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            full_message: dict[str, Any] | None = None
            tool_defs = [] if state.worker_needs_final_report else self._tools.tool_defs()
            if state.worker_flow is not None:
                tool_defs = state.worker_flow.filter_tool_defs(tool_defs)
            if state.stream_buffer is not None:
                state.stream_buffer.begin_round()

            label = "planner_stream" if "planner" in hook_name else "worker_stream"
            _log.info(
                "%s_start model=%s thinking=%s hook_name=%s",
                label, model, thinking, hook_name,
            )
            _first_event = True
            planner_hygiene = (
                PlannerStreamHygiene()
                if state.mode == "planner" and "planner" in hook_name
                else None
            )

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
                if planner_hygiene is not None and isinstance(ev, ContentDelta):
                    filtered_text = planner_hygiene.filter_delta(ev.text)
                    if not filtered_text:
                        continue
                    ev = ContentDelta(text=filtered_text)
                elif planner_hygiene is not None and isinstance(ev, Done):
                    flush_text = planner_hygiene.flush()
                    if flush_text:
                        on_event(ContentDelta(text=flush_text))
                    if isinstance(ev.full_message, dict):
                        content = ev.full_message.get("content")
                        if isinstance(content, str):
                            ev.full_message["content"] = planner_hygiene.sanitize_message_text(content)
                if state.mode == "worker" and state.stream_buffer is not None:
                    state.stream_buffer.capture_or_forward(ev, on_event)
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
            if state.worker_flow is not None:
                state.worker_flow.observe_assistant_message(full_message)
            if (
                not tool_calls
                and state.mode in {"planner", "single"}
                and state.task_completion_context
            ):
                content_text = assistant_message_text(full_message)
                if state.final_messages_after_completion >= 1:
                    if is_repetitive_completion_final(
                        content_text,
                        state.last_completion_final_text,
                    ):
                        return
                    return
                self._history.append_assistant(full_message)
                state.final_messages_after_completion += 1
                state.last_completion_final_text = content_text
                return

            # Worker final quarantine: hold candidate until validation gates pass
            if not tool_calls and state.mode == "worker":
                state.candidate_final_message = full_message
            else:
                self._history.append_assistant(full_message)
                if tool_calls and state.stream_buffer is not None:
                    state.stream_buffer.discard()

            if state.worker_needs_final_report:
                if not tool_calls:
                    if state.candidate_final_message is not None:
                        if _worker_final_report_missing_proof(
                            state,
                            state.candidate_final_message,
                        ):
                            self._history.append_user_text(
                                WORKER_FINAL_REPORT_PROOF_REQUIRED_TEXT
                            )
                            state.worker_final_report_proof_nudge_sent = True
                            state.discard_worker_candidate_final()
                            continue
                        self._history.append_assistant(state.candidate_final_message)
                        state.candidate_final_message = None
                    if state.stream_buffer is not None:
                        state.stream_buffer.flush(on_event)
                    return
                for tc in tool_calls:
                    fn = tc["function"]
                    name = fn["name"]
                    tool_call_id = tc["id"]
                    reason = (
                        str(state.worker_phase_boundary_info.get("reason"))
                        if state.worker_phase_boundary_info
                        else "worker_phase_boundary"
                    )
                    message = (
                        str(state.worker_phase_boundary_info.get("message"))
                        if state.worker_phase_boundary_info
                        else (
                            "Worker reached a recoverable phase boundary for this pass. "
                            "Produce the continuation report now."
                        )
                    )
                    info = {
                        "ok": False,
                        "limit_reached": bool(
                            state.worker_phase_boundary_info
                            and state.worker_phase_boundary_info.get("limit_reached")
                        ),
                        "loop_detected": bool(
                            state.worker_phase_boundary_info
                            and state.worker_phase_boundary_info.get("loop_detected")
                        ),
                        "recoverable": True,
                        "phase_boundary": True,
                        "reason": reason,
                        "tool": name,
                        "message": message,
                        "counts": state.limits.to_dict(),
                    }
                    self._append_limit_tool_result(tool_call_id, name, info, on_event)
                if state.stream_buffer is not None:
                    state.stream_buffer.discard()
                return

            if not tool_calls:
                if state.mode == "worker":
                    if has_terminal_syntax_failure(state.syntax_repair_required):
                        if not state.worker_recovery_nudge_sent:
                            diagnostic_parts = []
                            for path, s in state.syntax_repair_required.items():
                                if s.get("repair_failed") and s.get("error"):
                                    diagnostic_parts.append(f"{path}:\n{s['error']}")
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
                            state.worker_recovery_nudge_sent = True
                            state.discard_worker_candidate_final()
                            continue
                        failing_paths = sorted(
                            p for p, s in state.syntax_repair_required.items()
                            if s.get("repair_failed")
                        )
                        self._finish_worker_recoverable_followup(
                            on_event,
                            failure_class="syntax_invalid",
                            error="Python syntax still fails after two repair attempts.",
                            details={
                                "failing_files": failing_paths,
                                "suggested_next_tool": "dispatch_to_worker",
                                "suggested_next_action": (
                                    "Redispatch with a narrower edit target or "
                                    "different approach to the failing file."
                                ),
                                "planner_resolution_needed": True,
                                "worker_confusion_question": (
                                    "Worker could not repair Python syntax errors "
                                    "after two repair attempts"
                                    + (": " + ", ".join(failing_paths) if failing_paths else ".")
                                ),
                            },
                        )
                        return
                    # Carry import-verification paths forward for re-check
                    if state.import_verification_required:
                        for path in state.import_verification_required:
                            state.syntax_validation_required.add(path)
                    edit_recovery_pending = bool(
                        state.edit_fallback_required
                        or state.line_range_reread_required
                        or state.patch_invalid_syntax_required
                    )
                    syntax_repair_pending = bool(
                        syntax_repair_paths(state.syntax_repair_required)
                    )
                    if (
                        edit_recovery_pending
                        or syntax_repair_pending
                    ):
                        if not state.worker_recovery_nudge_sent:
                            if edit_recovery_pending:
                                if (
                                    state.patch_invalid_syntax_required
                                    and not state.edit_fallback_required
                                    and not state.line_range_reread_required
                                ):
                                    instruction = PATCH_CANDIDATE_INVALID_SYNTAX_ACTION
                                else:
                                    instruction = WORKER_EDIT_RECOVERY_INSTRUCTION
                            else:
                                # Distinguish craft-gate failures from terminal py_compile failures
                                any_repair_failed = any(
                                    s.get("repair_failed")
                                    for s in state.syntax_repair_required.values()
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
                                for path, s in state.syntax_repair_required.items():
                                    if (
                                        not s.get("awaiting_validation")
                                        and not s.get("repair_failed")
                                        and s.get("error")
                                    ):
                                        diagnostic_parts.append(f"{path}:\n{s['error']}")
                                diagnostic_text = "\n\n".join(diagnostic_parts)
                                if diagnostic_text:
                                    instruction += f"\n\nDiagnostic output:\n{diagnostic_text}"
                            self._history.append_user_text(instruction)
                            state.worker_recovery_nudge_sent = True
                            state.discard_worker_candidate_final()
                            continue
                        error_parts = [
                            "Worker stopped before recovering from a recoverable failure."
                        ]
                        details: dict[str, Any] = {}
                        if syntax_repair_pending:
                            sync_paths = sorted(
                                syntax_repair_paths(state.syntax_repair_required)
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
                                state.edit_fallback_required,
                                state.line_range_reread_required,
                            ))
                            if state.patch_invalid_syntax_required:
                                details["patch_invalid_syntax_paths"] = sorted(
                                    state.patch_invalid_syntax_required
                                )
                        details.update({
                            "suggested_next_tool": "dispatch_to_worker",
                            "suggested_next_action": (
                                "Redispatch with exact edit regions for "
                                "the files that failed to apply."
                            ),
                            "planner_resolution_needed": True,
                            "worker_confusion_question": (
                                "Worker exhausted edit-mechanics recovery; "
                                "could not apply edits for the targeted files."
                            ),
                        })
                        self._finish_worker_recoverable_followup(
                            on_event,
                            failure_class="worker_recovery_exhausted",
                            error="".join(error_parts),
                            details=details or None,
                        )
                        return
                    state.syntax_validation_required.difference_update(
                        path for path in set(state.syntax_validation_required)
                        if _is_validation_scratch_path(path)
                    )
                    if state.syntax_validation_required:
                        product_paths = sorted(
                            _normalize_worker_path(path) for path in state.syntax_validation_required
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
                                state.syntax_validation_required.clear()
                                if state.worker_flow is not None:
                                    state.worker_flow.mark_validation_satisfied()
                                # --- Import verification rung ---
                                import_ok, import_diag = run_focused_import_check(
                                    Path(self._tools.workspace_root),
                                    product_paths,
                                )
                                if not import_ok:
                                    for path in product_paths:
                                        state.import_verification_required.add(path)
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
                                    state.discard_worker_candidate_final()
                                    continue
                                else:
                                    for path in product_paths:
                                        state.import_verification_required.discard(path)
                                    # --- Dependent import verification rung ---
                                    fp_dep = fingerprint_paths(
                                        set(product_paths),
                                        self._tools.workspace_root,
                                    )
                                    try:
                                        gating_paths: list[str] = []
                                        if fp_dep and fp_dep == state.last_dependent_ok_fingerprint:
                                            pass
                                        else:
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
                                                        state.import_verification_required.add(path)
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
                                                    state.discard_worker_candidate_final()
                                                    continue
                                        if fp_dep and not gating_paths:
                                            state.last_dependent_ok_fingerprint = fp_dep
                                    except Exception:
                                        logging.getLogger(__name__).warning(
                                            "Dependent import check failed non-fatally",
                                            exc_info=True,
                                        )

                            else:
                                # Auto-py_compile failed — feed diagnostics back for repair
                                for path in product_paths:
                                    set_syntax_repair_state(state.syntax_repair_required, path, {
                                        "error": diagnostics,
                                        "failed_repairs": 0,
                                    })
                                state.syntax_validation_required.clear()
                                instruction = WORKER_AUTO_PY_COMPILE_INSTRUCTION.format(
                                    diagnostics=diagnostics,
                                )
                                self._history.append_user_text(instruction)
                                state.discard_worker_candidate_final()
                                continue
                    # --- Launch verification rung ---
                    if declared_run_command:
                        fp = fingerprint_paths(
                            state.worker_app_writes,
                            self._tools.workspace_root,
                        )
                        try:
                            if fp and fp == state.last_launch_ok_fingerprint:
                                emit_auto_launch_result(
                                    command=declared_run_command,
                                    ok=True,
                                    output="(skipped: no app-source change since last successful launch)",
                                    on_event=on_event,
                                    workspace_root=self._tools.workspace_root,
                                )
                            else:
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
                                    state.discard_worker_candidate_final()
                                    continue
                                if fp:
                                    state.last_launch_ok_fingerprint = fp
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
                        validation_runs = getattr(val_result, "runs", None)
                        if validation_runs:
                            emit_explicit_validation_runs(
                                runs=validation_runs,
                                on_event=on_event,
                                workspace_root=self._tools.workspace_root,
                            )
                        if not val_result.ok:
                            if not validation_runs:
                                emit_explicit_validation_result(
                                    command=val_result.command,
                                    ok=False,
                                    output=val_result.diagnostics,
                                    on_event=on_event,
                                    workspace_root=self._tools.workspace_root,
                                )
                            validation_key = val_result.command or "\n".join(
                                explicit_validation_commands
                            )
                            failure_count = state.explicit_validation_failure_counts.get(
                                validation_key,
                                0,
                            ) + 1
                            state.explicit_validation_failure_counts[validation_key] = failure_count
                            if failure_count > 1:
                                self._finish_worker_recoverable_followup(
                                    on_event,
                                    failure_class="product_validation_failed",
                                    error=(
                                        "Final acceptance validation still fails after one focused "
                                        "repair attempt."
                                    ),
                                    details={
                                        "command": val_result.command,
                                        "diagnostics": val_result.diagnostics,
                                        "suggested_next_tool": "dispatch_to_worker",
                                        "suggested_next_action": (
                                            "Redispatch a focused repair, or revise the "
                                            "acceptance command if it is misdeclared."
                                        ),
                                        "planner_resolution_needed": True,
                                        "worker_confusion_question": (
                                            "Worker could not make acceptance validation "
                                            "pass after one focused repair attempt"
                                            + (": " + str(val_result.command) if val_result.command else ".")
                                        ),
                                    },
                                )
                                return
                            instruction = WORKER_EXPLICIT_VALIDATION_FAILURE_INSTRUCTION.format(
                                command=val_result.command,
                                diagnostics=val_result.diagnostics,
                            )
                            self._history.append_user_text(instruction)
                            state.discard_worker_candidate_final()
                            continue
                        state.explicit_validation_failure_counts.clear()
                        if state.worker_flow is not None:
                            state.worker_flow.mark_validation_satisfied()
                    if (
                        state.worker_flow is not None
                        and state.worker_flow.requires_validation_before_final()
                    ):
                        if not state.worker_validation_nudge_sent:
                            self._history.append_user_text(
                                WORKER_FLOW_VALIDATION_REQUIRED_TEXT
                            )
                            state.worker_validation_nudge_sent = True
                            state.discard_worker_candidate_final()
                            continue
                        state.discard_worker_candidate_final()
                        self._finish_worker_recoverable_followup(
                            on_event,
                            failure_class="worker_validation_required",
                            error=(
                                "Worker stopped after changing files without running "
                                "focused validation after one validation nudge."
                            ),
                            details={
                                "suggested_next_tool": "run_terminal_command",
                                "suggested_next_action": (
                                    "Run the smallest relevant py_compile or pytest "
                                    "command, then provide the final report."
                                ),
                            },
                        )
                        return
                    flow_steering_action = self._handle_worker_flow_steering(
                        state,
                        on_event,
                    )
                    if flow_steering_action != "none":
                        state.discard_worker_candidate_final()
                        if flow_steering_action == "finished":
                            return
                        continue
                    zero_work_action = self._handle_worker_zero_work_final(
                        state,
                        on_event,
                    )
                    if zero_work_action != "none":
                        state.discard_worker_candidate_final()
                        if zero_work_action == "finished":
                            return
                        continue
                    quality_action = handle_worker_quality_gate(
                        state=state,
                        workspace_root=self._tools.workspace_root,
                        history=self._history,
                        on_event=on_event,
                        critic_cb=critic_cb,
                        worker_request=worker_dispatch_request,
                        dispatch_tool_call_id=dispatch_tool_call_id,
                    )
                    if quality_action != "none":
                        state.discard_worker_candidate_final()
                        if quality_action == "finished":
                            return
                        continue
                    # All gates passed — release candidate final and flush buffer
                    if state.candidate_final_message is not None:
                        if _worker_final_report_missing_proof(
                            state,
                            state.candidate_final_message,
                        ):
                            self._history.append_user_text(
                                WORKER_FINAL_REPORT_PROOF_REQUIRED_TEXT
                            )
                            state.worker_final_report_proof_nudge_sent = True
                            state.discard_worker_candidate_final()
                            continue
                        self._history.append_assistant(state.candidate_final_message)
                        state.candidate_final_message = None
                    if state.stream_buffer is not None:
                        state.stream_buffer.flush(on_event)
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

                flow_block = (
                    state.worker_flow.should_block_tool(name, args)
                    if state.worker_flow is not None
                    else None
                )
                if flow_block is not None:
                    tasks.append({
                        "id": tool_call_id,
                        "name": name,
                        "args": args,
                        "flow_block": flow_block,
                    })
                    continue

                allowed, limit_info = state.limits.check(name)
                if not allowed:
                    self._append_limit_tool_result(tool_call_id, name, limit_info, on_event)
                    if is_recoverable_phase_boundary(limit_info):
                        _worker_phase_boundary_info = limit_info
                    continue
                state.limits.record(name)
                if state.worker_flow is not None:
                    state.worker_flow.observe_tool_call(name, args)
                tasks.append({"id": tool_call_id, "name": name, "args": args})

            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            def process_task(task: dict[str, Any]) -> dict[str, Any]:
                nonlocal _terminal_dispatch, _worker_phase_boundary_info
                tool_call_id = task["id"]
                name = task["name"]
                args = task["args"]

                flow_block = task.get("flow_block")
                if isinstance(flow_block, dict):
                    return blocked_tool_result(tool_call_id, name, flow_block)

                if state.mode == "worker":
                    blocked = self._worker_recovery_block(
                        tool_call_id=tool_call_id,
                        name=name,
                        args=args,
                        edit_failed_shapes=state.edit_failed_shapes,
                        edit_fallback_required=state.edit_fallback_required,
                        recovery_block_counts=state.recovery_block_counts,
                        line_range_reread_required=state.line_range_reread_required,
                        worker_file_state=state.worker_file_state,
                        patch_failed_cycles=state.patch_failed_cycles,
                        patch_invalid_syntax_required=state.patch_invalid_syntax_required,
                        edit_retry_ledger=state.edit_retry_ledger,
                        syntax_repair_required=state.syntax_repair_required,
                        syntax_validation_required=state.syntax_validation_required,
                        write_attempts_by_path=state.write_attempts_by_path,
                    )
                    if blocked is not None:
                        blocked_payload = parse_tool_payload(str(blocked.get("result_payload", "")))
                        if is_recoverable_phase_boundary(blocked_payload):
                            _worker_phase_boundary_info = blocked_payload
                        return blocked

                if name == "dispatch_to_worker":
                    res = self._handle_dispatch_to_worker(
                        tool_call_id=tool_call_id,
                        args=args,
                        state=state,
                        dispatch_cb=dispatch_cb,
                        on_event=on_event,
                    )
                    if res.pop("terminal_dispatch", False):
                        _terminal_dispatch = True
                    return res

                if name == "run_and_watch":
                    loop_info = self._tool_runner.handle_run_and_watch(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        cancel_event=cancel_event,
                        declared_run_command=declared_run_command or "",
                    )
                    return {
                        "id": tool_call_id,
                        "skip": True,
                        "completed_tool_result_for_final": terminal_result_completed(loop_info),
                        "flow_result": {
                            "name": name,
                            "args": args,
                            "ok": _terminal_payload_ok(loop_info),
                            "result_payload": _terminal_payload(loop_info),
                        },
                    }

                if name == "run_terminal_command":
                    loop_info = self._tool_runner.handle_terminal_command(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        cancel_event=cancel_event,
                        mode=state.mode,
                        explicit_validation_commands=explicit_validation_commands,
                    )
                    if state.mode == "worker":
                        update_syntax_state_from_terminal(
                            args=args,
                            loop_info=loop_info,
                            workspace_root=Path(self._tools.workspace_root),
                            syntax_repair_required=state.syntax_repair_required,
                            syntax_validation_required=state.syntax_validation_required,
                            stale_validation_notes=state.stale_validation_notes,
                        )
                    if is_recoverable_phase_boundary(loop_info):
                        _worker_phase_boundary_info = loop_info
                    return {
                        "id": tool_call_id,
                        "skip": True,
                        "completed_tool_result_for_final": terminal_result_completed(loop_info),
                        "flow_result": {
                            "name": name,
                            "args": args,
                            "ok": _terminal_payload_ok(loop_info),
                            "result_payload": _terminal_payload(loop_info),
                        },
                    }

                if state.reject_all_for_turn and name in WRITE_TOOLS:
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
                        ),
                        "flow_result": {
                            "name": name,
                            "args": args,
                            "ok": False,
                            "result_payload": payload,
                        },
                    }

                exec_result = self._tools.execute(
                    name=name,
                    args=args,
                    approval_cb=approval_cb,
                    reject_all=False,
                )

                # Check rejection state after execute (approval_cb could set it)
                if exec_result.extras.get("approval") == "reject_all":
                    state.reject_all_for_turn = True

                tool_msg_content = exec_result.to_tool_message_content()
                if state.mode == "worker":
                    tool_msg_content = self._update_worker_recovery_state(
                        name=name,
                        args=args,
                        ok=exec_result.ok,
                        content=tool_msg_content,
                        edit_failed_shapes=state.edit_failed_shapes,
                        edit_fallback_required=state.edit_fallback_required,
                        line_range_reread_required=state.line_range_reread_required,
                        worker_file_state=state.worker_file_state,
                        patch_failed_cycles=state.patch_failed_cycles,
                        patch_invalid_syntax_required=state.patch_invalid_syntax_required,
                        edit_retry_ledger=state.edit_retry_ledger,
                        syntax_repair_required=state.syntax_repair_required,
                        syntax_validation_required=state.syntax_validation_required,
                        write_attempts_by_path=state.write_attempts_by_path,
                        worker_app_writes=state.worker_app_writes,
                    )

                loop_result = self._apply_loop_detection(
                    mode=state.mode,
                    name=name,
                    args=args,
                    ok=exec_result.ok,
                    result_payload=tool_msg_content,
                )
                tool_msg_content = loop_result["content"]
                loop_info = loop_result["info"]

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
                    "flow_result": {
                        "name": name,
                        "args": args,
                        "ok": exec_result.ok,
                        "result_payload": tool_msg_content,
                    },
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

            flow_steering_suppressed = False
            if state.stale_validation_notes:
                note_text = "\n".join(state.stale_validation_notes)
                self._history.append_user_text(note_text)
                flow_steering_suppressed = True

            completed_dispatch_for_final = False
            completed_tool_result_for_final = False
            planner_stale_read_files: list[str] = []
            planner_restart_needed = False
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
                    blocker_reason = str(res.get("blocker_reason", ""))
                    failure_constraint = res.get("failure_constraint", "")
                    result_obj = res.get("result")

                    # Internal planner handback: the dispatch failed in a
                    # recoverable way (not limit/repeated).  Append the
                    # failure constraint as a user message so the Planner
                    # sees it, but do NOT emit Done — the Planner loop
                    # restarts invisibly.
                    is_internal = (
                        bool(failure_constraint)
                        and blocker_reason not in ("limit", "repeated")
                        and isinstance(result_obj, WorkerDispatchResult)
                        and not result_obj.cancelled
                    )
                    if is_internal:
                        extras = result_obj.extras or {}
                        if not (
                            extras.get("user_visible_blocker")
                            or extras.get("user_only_blocker")
                            or extras.get("terminal_environment_blocker")
                        ):
                            self._history.append_user_text(failure_constraint)
                            planner_restart_needed = True
                            break

                    # Terminal / user-visible blocker: surface it.
                    self._append_dispatch_blocker_message(
                        res["result"], blocker_reason, on_event,
                        failure_constraint,
                    )
                    return
                if res.get("completed_dispatch_for_final"):
                    completed_dispatch_for_final = True
                if res.get("completed_tool_result_for_final"):
                    completed_tool_result_for_final = True
                if state.worker_flow is not None and res.get("flow_result"):
                    flow_result = res["flow_result"]
                    before_write_actions = int(state.worker_flow.state.write_actions)
                    before_validation_actions = int(state.worker_flow.state.validation_actions)
                    state.worker_flow.observe_tool_result(
                        flow_result.get("name", task["name"]),
                        flow_result.get("args", task["args"]),
                        flow_result.get("ok"),
                        flow_result.get("result_payload"),
                    )
                    if (
                        state.worker_flow.state.write_actions > before_write_actions
                        or state.worker_flow.state.validation_actions > before_validation_actions
                    ):
                        state.worker_flow_nudge_sent = False
                if res.get("skip"):
                    continue

                if "result_payload" in res:
                    self._history.append_tool_result(task["id"], res["result_payload"])
                    on_event(res["event"])

            if planner_restart_needed:
                continue

            self._planner_refresh.handle_post_write_notices(
                self._history, planner_stale_read_files
            )

            if _worker_phase_boundary_info is not None:
                state.worker_phase_boundary_info = _worker_phase_boundary_info
                if state.worker_phase_boundary_info.get("message"):
                    self._history.append_user_text(str(state.worker_phase_boundary_info["message"]))
                state.worker_needs_final_report = True
                continue

            if completed_dispatch_for_final:
                return
            if completed_tool_result_for_final:
                state.task_completion_context = True
                continue

            # If research completed, stop the loop.
            # The Research Completed card is the final user-facing result.
            if _terminal_dispatch:
                return

            if state.worker_flow is not None and not flow_steering_suppressed:
                flow_steering_action = self._handle_worker_flow_steering(
                    state,
                    on_event,
                )
                if flow_steering_action == "finished":
                    return

    def _finish_worker_unrecoverable(
        self,
        on_event: EventCallback,
        *,
        failure_class: str,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        content, full_message = build_worker_unrecoverable_message(
            failure_class=failure_class,
            error=error,
            details=details,
        )
        self._history.append_assistant(full_message)
        on_event(ContentDelta(text=content))
        on_event(Done(finish_reason="stop", full_message=full_message))

    def _finish_worker_recoverable_followup(
        self,
        on_event: EventCallback,
        *,
        failure_class: str,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        content, full_message = build_worker_recoverable_followup_message(
            failure_class=failure_class,
            error=error,
            details=details,
        )
        self._history.append_assistant(full_message)
        on_event(ContentDelta(text=content))
        on_event(Done(finish_reason="stop", full_message=full_message))

    def _handle_worker_flow_steering(
        self,
        state: _SendState,
        on_event: EventCallback,
    ) -> str:
        """Steer Worker Flow without turning zero-work orientation into follow-up."""
        if state.worker_flow is None:
            return "none"
        reason = str(
            getattr(state.worker_flow.state, "pending_steering_reason", "")
            or "worker_flow"
        )
        steering = state.worker_flow.pop_pending_steering()
        if not steering:
            return "none"
        state.worker_flow_last_reason = reason
        state.worker_flow_last_steering = steering
        if state.worker_flow_nudge_sent:
            if _worker_has_zero_applied_writes(state) and not _worker_has_attempted_write(state):
                if _candidate_final_has_real_zero_work_blocker(state.candidate_final_message):
                    return "none"
                if not state.worker_flow_zero_work_recovery_sent:
                    self._append_worker_zero_work_recovery(
                        state,
                        reason=reason,
                        steering=steering,
                    )
                    return "nudged"
                self._finish_worker_recoverable_followup(
                    on_event,
                    failure_class="worker_flow_zero_work_no_progress",
                    error=(
                        "Worker could not make progress after internal zero-work "
                        "recovery. Handing step back for planner resolution."
                    ),
                    details={
                        "reason": reason,
                        "steering": steering,
                        "suggested_next_tool": "dispatch_to_worker",
                        "suggested_next_action": (
                            "Redispatch with a narrower target, exact edit region, "
                            "or explicit blocker resolution."
                        ),
                        "planner_resolution_needed": True,
                        "worker_confusion_question": (
                            f"Worker could not identify a safe first edit for: "
                            f"{steering}"
                        ),
                    },
                )
                return "finished"
            self._finish_worker_recoverable_followup(
                on_event,
                failure_class="worker_flow_thrash",
                error=(
                    "Worker kept re-orienting after a Worker Flow nudge instead "
                    "of making progress with an edit or validation action."
                ),
                details={
                    "reason": reason,
                    "steering": steering,
                    "counts": state.limits.to_dict(),
                    "suggested_next_tool": "dispatch_to_worker",
                    "suggested_next_action": (
                        "Redispatch with a narrower target, exact edit region, "
                        "or explicit blocker resolution."
                    ),
                },
            )
            return "finished"
        self._history.append_user_text(steering)
        state.worker_flow_nudge_sent = True
        return "nudged"

    def _handle_worker_zero_work_final(
        self,
        state: _SendState,
        on_event: EventCallback,
    ) -> str:
        """Recover or fail internally when Worker tries to finish with no work."""
        if not _worker_has_zero_applied_writes(state):
            return "none"
        if _worker_has_attempted_write(state):
            return "none"
        if state.reject_all_for_turn:
            return "none"
        if _candidate_final_has_real_zero_work_blocker(state.candidate_final_message):
            return "none"
        if not state.worker_flow_zero_work_recovery_sent:
            self._append_worker_zero_work_recovery(
                state,
                reason=state.worker_flow_last_reason or "zero_work_final",
                steering=state.worker_flow_last_steering,
            )
            return "nudged"
        self._finish_worker_recoverable_followup(
            on_event,
            failure_class="worker_flow_zero_work_no_progress",
            error=(
                "Worker could not make progress after internal zero-work "
                "recovery. Handing step back for planner resolution."
            ),
            details={
                "reason": state.worker_flow_last_reason or "zero_work_final",
                "steering": state.worker_flow_last_steering,
                "suggested_next_tool": "dispatch_to_worker",
                "suggested_next_action": (
                    "Redispatch with a narrower target, exact edit region, "
                    "or explicit blocker resolution."
                ),
                "planner_resolution_needed": True,
                "worker_confusion_question": (
                    f"Worker could not identify a safe first edit for: "
                    f"{state.worker_flow_last_steering}"
                ),
            },
        )
        return "finished"

    def _append_worker_zero_work_recovery(
        self,
        state: _SendState,
        *,
        reason: str,
        steering: str,
    ) -> None:
        details = [
            WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT,
            "",
            "Internal recovery context:",
            f"- worker_flow_reason: {reason}",
        ]
        if steering:
            details.append(f"- last_steering: {steering}")
        self._history.append_user_text("\n".join(details))
        state.worker_flow_zero_work_recovery_sent = True
        state.worker_flow_nudge_sent = True
        state.worker_flow_last_reason = reason
        state.worker_flow_last_steering = steering

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
        edit_retry_ledger: EditRetryLedger | None = None,
    ) -> dict[str, Any] | None:
        return worker_recovery_block(
            self._tools.workspace_root,
            tool_call_id=tool_call_id,
            name=name,
            args=args,
            edit_failed_shapes=edit_failed_shapes,
            edit_fallback_required=edit_fallback_required,
            recovery_block_counts=recovery_block_counts,
            line_range_reread_required=line_range_reread_required,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
            write_attempts_by_path=write_attempts_by_path,
            worker_file_state=worker_file_state,
            patch_failed_cycles=patch_failed_cycles,
            patch_invalid_syntax_required=patch_invalid_syntax_required,
            edit_retry_ledger=edit_retry_ledger,
        )





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
        worker_app_writes: set[str] | None = None,
        worker_file_state: dict[str, dict[str, Any]] | None = None,
        patch_failed_cycles: dict[str, int] | None = None,
        patch_invalid_syntax_required: dict[str, dict[str, Any]] | None = None,
        edit_retry_ledger: EditRetryLedger | None = None,
    ) -> str:
        return update_worker_recovery_state(
            self._tools.workspace_root,
            name=name,
            args=args,
            ok=ok,
            content=content,
            edit_failed_shapes=edit_failed_shapes,
            edit_fallback_required=edit_fallback_required,
            line_range_reread_required=line_range_reread_required,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
            write_attempts_by_path=write_attempts_by_path,
            worker_app_writes=worker_app_writes,
            worker_file_state=worker_file_state,
            patch_failed_cycles=patch_failed_cycles,
            patch_invalid_syntax_required=patch_invalid_syntax_required,
            edit_retry_ledger=edit_retry_ledger,
        )

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

    def _append_pure_research_dispatch_block(
        self,
        tool_call_id: str,
        on_event: EventCallback,
    ) -> WorkerDispatchResult:
        summary = (
            "Worker was not started because this turn is a pure external "
            "research request. Run web research and answer from sourced evidence."
        )
        result = WorkerDispatchResult(
            ok=False,
            summary=summary,
            recoverable=True,
            extras={
                "dispatch_not_started": True,
                "pure_research": True,
                "research_route": "answer_only",
            },
        )
        payload = json.dumps(
            result.to_tool_payload(),
            ensure_ascii=False,
        )
        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="dispatch_to_worker",
                ok=True,
                result=payload,
                extras={
                    "dispatch_not_started": True,
                    "pure_research": True,
                    "recoverable": True,
                    "summary": summary,
                },
            )
        )
        return result

    def _append_dispatch_blocker_message(
        self,
        result: WorkerDispatchResult,
        reason: str,
        on_event: EventCallback,
        failure_constraint: str = "",
    ) -> None:
        if failure_constraint:
            self._history.append_user_text(failure_constraint)
        # Suppress visible card. Still emit Done with empty content so the
        # planner stream closes cleanly. No ContentDelta, no history append.
        on_event(Done(
            finish_reason="stop",
            full_message={
                "role": "assistant",
                "content": "",
                "reasoning_content": None,
            },
        ))

    def _handle_dispatch_to_worker(
        self,
        *,
        tool_call_id: str,
        args: dict[str, Any],
        state: _SendState,
        dispatch_cb: DispatchCallback | None,
        on_event: EventCallback,
    ) -> dict[str, Any]:
        if (
            state.research_policy.route == ANSWER_ONLY
            and not _dispatch_args_look_like_local_code_work(args)
        ):
            result = self._append_pure_research_dispatch_block(
                tool_call_id=tool_call_id,
                on_event=on_event,
            )
            action = classify_failed_worker_dispatch(
                args=args,
                result=result,
                failures=state.worker_dispatch_failures,
                failed_attempts=state.worker_redispatches,
            )
            if action["counts_as_attempt"]:
                state.worker_redispatches += 1
            blocker_reason = action["blocker_reason"]
            failure_constraint = action.get("failure_constraint", "")
            if blocker_reason or failure_constraint:
                return {
                    "id": tool_call_id,
                    "blocker": True,
                    "result": result,
                    "blocker_reason": blocker_reason,
                    "terminal_dispatch": False,
                    "failure_constraint": failure_constraint,
                }
            return {
                "id": tool_call_id,
                "skip": True,
                "completed_dispatch_for_final": False,
                "terminal_dispatch": False,
            }
        result = self._tool_runner.handle_dispatch(
            tool_call_id=tool_call_id,
            args=args,
            on_event=on_event,
            dispatch_cb=dispatch_cb,
        )
        terminal_dispatch = False
        if result is not None and not result.cancelled:
            if result.ok:
                terminal_dispatch = True
            else:
                action = classify_failed_worker_dispatch(
                    args=args,
                    result=result,
                    failures=state.worker_dispatch_failures,
                    failed_attempts=state.worker_redispatches,
                )
                if action["counts_as_attempt"]:
                    state.worker_redispatches += 1
                blocker_reason = action["blocker_reason"]
                failure_constraint = action.get("failure_constraint", "")
                if blocker_reason or failure_constraint:
                    return {
                        "id": tool_call_id,
                        "blocker": True,
                        "result": result,
                        "blocker_reason": blocker_reason,
                        "failure_constraint": failure_constraint,
                        "planner_stale_read_files": (
                            list(result.modified_files)
                            if result.modified_files
                            else []
                        ),
                        "terminal_dispatch": False,
                    }
        return {
            "id": tool_call_id,
            "skip": True,
            "completed_dispatch_for_final": worker_dispatch_is_terminal(result),
            "planner_stale_read_files": (
                list(result.modified_files) if result and result.modified_files else []
            ),
            "terminal_dispatch": terminal_dispatch,
        }

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


def _latest_user_text(history: History) -> str:
    for message in reversed(history.messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
    return ""


def _dispatch_args_look_like_local_code_work(args: dict[str, Any]) -> bool:
    """Detect concrete local code dispatches despite a bad research route."""
    if not isinstance(args, dict):
        return False

    if not any(_looks_like_local_path(path) for path in _dispatch_local_path_candidates(args)):
        return False

    intent_text = " ".join(
        str(args.get(key) or "")
        for key in ("spec", "goal", "acceptance")
    )
    return bool(_LOCAL_CODE_INTENT_RE.search(intent_text))


def _dispatch_local_path_candidates(args: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []

    files = args.get("files")
    if isinstance(files, list):
        candidates.extend(files)

    target_regions = args.get("target_regions")
    if isinstance(target_regions, list):
        for region in target_regions:
            if isinstance(region, dict):
                candidates.append(region.get("path"))

    required_outputs = args.get("required_outputs")
    if isinstance(required_outputs, list):
        candidates.extend(required_outputs)

    for key in ("spec", "goal", "acceptance", "summary"):
        candidates.extend(_extract_local_path_mentions(str(args.get(key) or "")))

    return candidates


def _extract_local_path_mentions(text: str) -> list[str]:
    mentions: list[str] = []
    for match in re.finditer(r"(?<![\w:/.-])([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+)(?![\w.-])", text):
        mentions.append(match.group(1))
    for match in re.finditer(r"(?<![\w.-])([A-Za-z0-9_.-]+\.(?:py|pyw|ts|tsx|js|jsx|json|toml|yaml|yml|md|txt|css|scss|html|gd|cs|java|go|rs|cpp|c|h|hpp))(?![\w.-])", text):
        mentions.append(match.group(1))
    return mentions


def _looks_like_local_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path = value.strip()
    if not path or "\n" in path or "\r" in path:
        return False
    lowered = path.lower()
    if "://" in lowered or lowered.startswith(("www.", "http:", "https:")):
        return False
    return (
        "/" in path
        or "\\" in path
        or "." in Path(path).name
        or path.startswith(".")
    )


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
