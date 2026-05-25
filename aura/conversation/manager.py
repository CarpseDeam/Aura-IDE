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

import hashlib
import subprocess
import sys
import json
import re
import threading
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
from aura.hooks import hooks
from aura.config import ModelId, ThinkingMode
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tool_limits import (
    MAX_WORKER_REDISPATCHES_PER_USER_TURN,
    ToolLimitState,
    WRITE_TOOLS,
    limit_reached_payload,
)
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
)
from aura.conversation.tools.registry import ToolRegistry

EventCallback = Callable[[Event], None]

EDIT_MECHANICS_FAILURE_CLASSES = {
    "edit_mechanics_symbol_not_found",
    "edit_mechanics_old_str_not_found",
    "edit_mechanics_ambiguous_match",
}

WORKER_EDIT_RECOVERY_INSTRUCTION = (
    "Previous edit failed recoverably. Re-read the file, then use edit_line_range "
    "with exact line numbers, or use write_file if a full replacement is safer. "
    "Finish only after the edit is applied and touched Python files pass py_compile."
)

WORKER_AUTO_PY_COMPILE_INSTRUCTION = (
    "Focused py_compile failed on the following Python file(s). "
    "Re-read and repair the file(s), then run python -m py_compile again. "
    "Finish only after py_compile passes.\n\n"
    "Diagnostic output:\n{diagnostics}"
)

WORKER_COMPILER_REPAIR_INSTRUCTION = (
    "Craft reviewed the proposed patch and returned repair notes. Re-read the "
    "affected file, repair the proposal using the notes below, then retry the "
    "patch once. Normal inspection and validation tools remain available."
)


def _normalize_worker_path(path: str) -> str:
    normalized = str(path).replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _is_validation_scratch_path(path: str) -> bool:
    normalized = _normalize_worker_path(path)
    if not (normalized.startswith(".aura/tmp/") and normalized.endswith(".py")):
        return False
    name = normalized.rsplit("/", 1)[-1]
    return name.startswith(("dump", "_check", "check", "tmp"))


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

    @property
    def history(self) -> History:
        return self._history

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    def send(
        self,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
        dispatch_cb: DispatchCallback | None = None,
        temperature: float = 0.7,
        max_tool_rounds: int | None = None,
        hook_name: str = 'generate_planner_code',
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
        rounds_used = 0
        worker_needs_final_report = False
        worker_phase_boundary_info: dict[str, Any] | None = None
        worker_redispatches = 0
        worker_dispatch_failures: dict[str, int] = {}
        edit_failed_shapes: set[str] = set()
        edit_fallback_required: dict[str, dict[str, Any]] = {}
        recovery_block_counts: dict[str, int] = {}
        line_range_reread_required: dict[str, dict[str, Any]] = {}
        syntax_repair_required: dict[str, dict[str, Any]] = {}
        syntax_validation_required: set[str] = set()
        compiler_repair_required: dict[str, dict[str, Any]] = {}
        worker_recovery_nudge_sent = False
        patch_quality_unresolved: dict[str, Any] | None = None

        while True:
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

            for ev in hooks.trigger(
                hook_name,
                messages=self._history.for_api(),
                tools=tool_defs,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
                temperature=temperature,
            ):
                on_event(ev)
                if isinstance(ev, Done):
                    full_message = ev.full_message
                if isinstance(ev, ApiError):
                    return  # surface and stop

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

            self._history.append_assistant(full_message)

            tool_calls = full_message.get("tool_calls") or []
            if worker_needs_final_report:
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
                return

            if not tool_calls:
                if mode == "worker":
                    if self._has_terminal_syntax_failure(syntax_repair_required):
                        self._finish_worker_unrecoverable(
                            on_event,
                            failure_class="syntax_invalid",
                            error="Python syntax still fails after one repair attempt.",
                        )
                        return
                    if patch_quality_unresolved is not None:
                        self._finish_worker_unrecoverable(
                            on_event,
                            failure_class="patch_quality_unresolved",
                            error=str(
                                patch_quality_unresolved.get("error")
                                or "Patch quality needs repair."
                            ),
                        )
                        return
                    edit_recovery_pending = bool(
                        edit_fallback_required or line_range_reread_required
                    )
                    syntax_repair_pending = bool(
                        self._syntax_repair_paths(syntax_repair_required)
                    )
                    compiler_repair_pending = bool(
                        self._compiler_repair_paths(compiler_repair_required)
                    )
                    if edit_recovery_pending or syntax_repair_pending or compiler_repair_pending:
                        if not worker_recovery_nudge_sent:
                            instruction = (
                                WORKER_COMPILER_REPAIR_INSTRUCTION
                                if compiler_repair_pending
                                else WORKER_EDIT_RECOVERY_INSTRUCTION
                                if edit_recovery_pending
                                else (
                                    "Previous py_compile failed. Re-read the touched "
                                    "Python file, repair it with edit_line_range or "
                                    "write_file, then run python -m py_compile again. "
                                    "Finish only after py_compile passes."
                                )
                            )
                            self._history.append_user_text(instruction)
                            worker_recovery_nudge_sent = True
                            continue
                        if compiler_repair_pending:
                            self._finish_worker_unrecoverable(
                                on_event,
                                failure_class="worker_compiler_repair_exhausted",
                                error=(
                                    "Worker stopped before repairing a recoverable "
                                    "Craft/compiler rejection."
                                ),
                            )
                            return
                        self._finish_worker_unrecoverable(
                            on_event,
                            failure_class="worker_recovery_exhausted",
                            error=(
                                "Worker stopped before recovering from a recoverable edit "
                                "mechanics failure."
                            ),
                        )
                        return
                    syntax_validation_required.difference_update(
                        path for path in set(syntax_validation_required)
                        if _is_validation_scratch_path(path)
                    )
                    if syntax_validation_required:
                        product_paths = sorted(
                            path for path in syntax_validation_required
                            if not _is_validation_scratch_path(path)
                        )
                        if product_paths:
                            all_ok, diagnostics = self._run_focused_py_compile(product_paths)
                            self._emit_auto_py_compile_result(
                                paths=product_paths,
                                ok=all_ok,
                                diagnostics=diagnostics,
                                on_event=on_event,
                            )
                            if all_ok:
                                syntax_validation_required.clear()
                            else:
                                # Auto-py_compile failed — feed diagnostics back for repair
                                for path in product_paths:
                                    syntax_repair_required[path] = {
                                        "error": diagnostics,
                                        "failed_repairs": 0,
                                    }
                                syntax_validation_required.clear()
                                instruction = WORKER_AUTO_PY_COMPILE_INSTRUCTION.format(
                                    diagnostics=diagnostics,
                                )
                                self._history.append_user_text(instruction)
                                continue
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
                    if self._is_recoverable_phase_boundary(limit_info):
                        _worker_phase_boundary_info = limit_info
                    continue
                limits.record(name)
                tasks.append({"id": tool_call_id, "name": name, "args": args})

            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            def process_task(task: dict[str, Any]) -> dict[str, Any]:
                nonlocal _terminal_dispatch, _worker_phase_boundary_info, reject_all_for_turn, worker_redispatches, patch_quality_unresolved
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
                        syntax_repair_required=syntax_repair_required,
                        syntax_validation_required=syntax_validation_required,
                        compiler_repair_required=compiler_repair_required,
                    )
                    if blocked is not None:
                        blocked_payload = self._parse_tool_payload(str(blocked.get("result_payload", "")))
                        if self._is_recoverable_phase_boundary(blocked_payload):
                            _worker_phase_boundary_info = blocked_payload
                        return blocked

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
                            action = self._classify_failed_worker_dispatch(
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
                                }
                    return {"id": tool_call_id, "skip": True}

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

                if name == "run_terminal_command":
                    loop_info = self._tool_runner.handle_terminal_command(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        cancel_event=cancel_event,
                        mode=mode,
                    )
                    if mode == "worker":
                        self._update_syntax_state_from_terminal(
                            args=args,
                            loop_info=loop_info,
                            syntax_repair_required=syntax_repair_required,
                            syntax_validation_required=syntax_validation_required,
                        )
                    if self._is_recoverable_phase_boundary(loop_info):
                        _worker_phase_boundary_info = loop_info
                    return {"id": tool_call_id, "skip": True}

                if reject_all_for_turn and name in WRITE_TOOLS:
                    payload = json.dumps(
                        {"ok": False, "error": "User rejected all writes in this turn.", "failure_class": "approval_rejected"}
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
                        syntax_repair_required=syntax_repair_required,
                        syntax_validation_required=syntax_validation_required,
                        compiler_repair_required=compiler_repair_required,
                    )
                    parsed_after_recovery = self._parse_tool_payload(tool_msg_content)
                    if isinstance(parsed_after_recovery, dict) and parsed_after_recovery.get("patch_quality_unresolved"):
                        patch_quality_unresolved = parsed_after_recovery

                loop_result = self._apply_loop_detection(
                    mode=mode,
                    name=name,
                    args=args,
                    ok=exec_result.ok,
                    result_payload=tool_msg_content,
                )
                tool_msg_content = loop_result["content"]
                loop_info = loop_result["info"]

                if self._is_recoverable_phase_boundary(loop_info):
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
                    "quality_instruction": (
                        self._quality_bounce_instruction(parsed_after_recovery)
                        if mode == "worker"
                        and isinstance(parsed_after_recovery, dict)
                        and parsed_after_recovery.get("quality_bounce")
                        and not parsed_after_recovery.get("patch_quality_unresolved")
                        else ""
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

            quality_instructions: list[str] = []
            for task in tasks:
                if cancel_event.is_set():
                    self._cleanup_cancelled(on_event)
                    return

                res = results_by_id.get(task["id"])
                if not res:
                    continue

                if res.get("blocker"):
                    self._append_dispatch_blocker_message(
                        res["result"], str(res.get("blocker_reason", "")), on_event
                    )
                    return
                if res.get("skip"):
                    continue

                if "result_payload" in res:
                    self._history.append_tool_result(task["id"], res["result_payload"])
                    on_event(res["event"])
                    instruction = res.get("quality_instruction")
                    if isinstance(instruction, str) and instruction:
                        quality_instructions.append(instruction)

            for instruction in quality_instructions:
                self._history.append_user_text(instruction)

            if patch_quality_unresolved is not None:
                self._finish_worker_unrecoverable(
                    on_event,
                    failure_class="patch_quality_unresolved",
                    error=str(
                        patch_quality_unresolved.get("error")
                        or "Patch quality needs repair."
                    ),
                )
                return

            if _worker_phase_boundary_info is not None:
                worker_phase_boundary_info = _worker_phase_boundary_info
                worker_needs_final_report = True
                continue

            # If any dispatch_to_worker or run_research completed, stop the loop.
            # The Worker Completed card is the final user-facing result.
            if _terminal_dispatch:
                return

    @staticmethod
    def _syntax_repair_paths(
        syntax_repair_required: dict[str, dict[str, Any]],
    ) -> set[str]:
        return {
            path
            for path, state in syntax_repair_required.items()
            if not state.get("awaiting_validation")
        }

    @staticmethod
    def _compiler_repair_paths(
        compiler_repair_required: dict[str, dict[str, Any]],
    ) -> set[str]:
        return {
            path
            for path, state in compiler_repair_required.items()
            if not state.get("repair_failed") and not state.get("quality_bounce")
        }

    @staticmethod
    def _has_compiler_repair_failure(
        compiler_repair_required: dict[str, dict[str, Any]],
    ) -> bool:
        return any(
            state.get("repair_failed") and not state.get("quality_bounce")
            for state in compiler_repair_required.values()
        )

    @staticmethod
    def _has_terminal_syntax_failure(
        syntax_repair_required: dict[str, dict[str, Any]],
    ) -> bool:
        return any(state.get("repair_failed") for state in syntax_repair_required.values())

    def _finish_worker_unrecoverable(
        self,
        on_event: EventCallback,
        *,
        failure_class: str,
        error: str,
    ) -> None:
        payload = {
            "ok": False,
            "failure_class": failure_class,
            "error": error,
        }
        content = json.dumps(payload, ensure_ascii=False)
        full_message = {
            "role": "assistant",
            "content": content,
            "reasoning_content": None,
        }
        self._history.append_assistant(full_message)
        on_event(ContentDelta(text=content))
        on_event(Done(finish_reason="stop", full_message=full_message))

    def _run_focused_py_compile(
        self,
        paths: list[str],
    ) -> tuple[bool, str]:
        """Run python -m py_compile on each touched product Python file.

        Returns (all_succeeded, combined_output).
        Uses sys.executable, cwd=workspace root, timeout=30s.
        Normalizes paths safely (backslash/slash, strip leading "./").
        Preserves dot-prefixed directories like .aura.
        """
        if not paths:
            return True, ""
        workspace_root = self._tools.workspace_root
        compiler = sys.executable
        outputs: list[str] = []
        all_ok = True
        for path in sorted(paths):
            normalized = _normalize_worker_path(path)
            if _is_validation_scratch_path(normalized):
                continue
            full_path = Path(workspace_root) / normalized
            if not full_path.exists():
                outputs.append(f"{normalized}: file not found — cannot py_compile")
                all_ok = False
                continue
            try:
                result = subprocess.run(
                    [compiler, "-m", "py_compile", str(full_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=workspace_root,
                )
                if result.returncode != 0:
                    all_ok = False
                    err = result.stderr.strip() or result.stdout.strip() or "py_compile failed"
                    outputs.append(f"{normalized}: {err}")
                else:
                    outputs.append(f"{normalized}: ok")
            except subprocess.TimeoutExpired:
                all_ok = False
                outputs.append(f"{normalized}: timed out after 30s")
            except FileNotFoundError:
                all_ok = False
                outputs.append(f"{normalized}: sys.executable not found")
            except OSError as exc:
                all_ok = False
                outputs.append(f"{normalized}: OSError: {exc}")
        combined = "\n".join(outputs)
        return all_ok, combined

    @staticmethod
    def _emit_auto_py_compile_result(
        *,
        paths: list[str],
        ok: bool,
        diagnostics: str,
        on_event: EventCallback,
    ) -> None:
        product_paths = [
            _normalize_worker_path(path)
            for path in paths
            if not _is_validation_scratch_path(path)
        ]
        if not product_paths:
            return
        command = "python -m py_compile " + " ".join(product_paths)
        payload = {
            "ok": ok,
            "command": command,
            "exit_code": 0 if ok else 1,
            "output": diagnostics,
            "auto_validation": True,
        }
        content = json.dumps(payload, ensure_ascii=False)
        on_event(
            ToolResult(
                tool_call_id="auto_py_compile",
                name="run_terminal_command",
                ok=ok,
                result=content,
            )
        )

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
        compiler_repair_required: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        path = self._tool_path(name, args)
        if self._has_compiler_repair_failure(compiler_repair_required):
            target = sorted(
                path for path, state in compiler_repair_required.items()
                if state.get("repair_failed")
            )[0]
            payload = self._recovery_payload(
                path=target,
                failure_class="compiler_rejected",
                error="Patch quality needs repair after one retry.",
                suggested_next_tool="",
                suggested_next_action="Stop tool use and report the compiler rejection.",
                recoverable=False,
            )
            self._record_recovery_block(payload, f"compiler-failed:{target}:{name}", recovery_block_counts)
            return self._blocked_tool_result(tool_call_id, name, payload)

        compiler_paths = self._compiler_repair_paths(compiler_repair_required)
        if compiler_paths and not self._compiler_repair_tool_allowed(
            name, args, compiler_repair_required
        ):
            target = sorted(compiler_paths)[0]
            state = compiler_repair_required.get(target, {})
            repair_failed = bool(state.get("repair_failed"))
            payload = self._recovery_payload(
                path=target,
                failure_class="compiler_rejected",
                error=(
                    "Patch quality needs repair after one retry."
                    if repair_failed
                    else (
                        "Craft/compiler rejected the proposed code. Re-read the file, "
                        "repair the checker issue once, and retry with a different "
                        "write tactic."
                    )
                ),
                suggested_next_tool=str(
                    state.get("suggested_next_tool")
                    or self._alternate_write_tactic(str(state.get("tool") or name))
                ),
                suggested_next_action=(
                    "Repair the proposed code once using the precise checker error; "
                    "do not narrate. Retry with a different write tactic."
                ),
                recoverable=not repair_failed,
            )
            payload["previous_error"] = state.get("error", "")
            self._record_recovery_block(payload, f"compiler:{target}:{name}", recovery_block_counts)
            return self._blocked_tool_result(tool_call_id, name, payload)

        syntax_paths = self._syntax_repair_paths(syntax_repair_required)
        if syntax_paths and not self._syntax_repair_tool_allowed(name, args, syntax_paths):
            target = sorted(syntax_paths)[0]
            state = syntax_repair_required.get(target, {})
            repair_failed = bool(state.get("repair_failed"))
            payload = self._recovery_payload(
                path=target,
                failure_class="syntax_invalid",
                error=(
                    f"Python syntax is invalid in {target}. "
                    + (
                        "Syntax still fails after one repair attempt."
                        if repair_failed
                        else "Repair that file and pass py_compile before any unrelated tool call."
                    )
                ),
                suggested_next_tool="edit_line_range",
                suggested_next_action="Repair the touched file, then run python -m py_compile on it before continuing validation.",
                recoverable=not repair_failed,
            )
            self._record_recovery_block(payload, f"syntax:{target}:{name}", recovery_block_counts)
            return self._blocked_tool_result(tool_call_id, name, payload)

        if name == "edit_line_range" and path in line_range_reread_required:
            payload = self._recovery_payload(
                path=path,
                failure_class="edit_mechanics_stale_line_range",
                error="Stale line range after a failed edit_line_range. Re-read the file before retrying line-range editing.",
                suggested_next_tool="read_file",
                suggested_next_action="Re-read the file, then make one corrected edit_line_range attempt or use write_file.",
            )
            self._record_recovery_block(payload, f"line-range-reread:{path}", recovery_block_counts)
            return self._blocked_tool_result(tool_call_id, name, payload)

        if name in ("edit_file", "edit_symbol") and path in edit_fallback_required:
            prior = edit_fallback_required[path]
            block_key = self._edit_shape_signature(name, args)
            payload = self._recovery_payload(
                path=path,
                failure_class=str(prior.get("failure_class") or self._default_edit_failure_class(name)),
                error="Repeated failed edit tactic. Do not retry this edit shape. Re-read the file and use edit_line_range or write_file.",
                suggested_next_tool="edit_line_range",
                suggested_next_action="Use read_file/read_file_outline, then edit_line_range with exact line numbers, or use write_file.",
            )
            payload["previous_error"] = prior.get("error", "")
            self._record_recovery_block(payload, block_key, recovery_block_counts)
            return self._blocked_tool_result(tool_call_id, name, payload)

        if name in ("edit_file", "edit_symbol", "edit_line_range"):
            shape = self._edit_shape_signature(name, args)
            if shape in edit_failed_shapes:
                payload = self._recovery_payload(
                    path=path,
                    failure_class=self._default_edit_failure_class(name),
                    error="Repeated failed edit tactic. Do not retry this edit shape. Re-read the file and use edit_line_range or write_file.",
                    suggested_next_tool="edit_line_range",
                    suggested_next_action="Use read_file/read_file_outline, then edit_line_range with exact line numbers, or use write_file.",
                )
                self._record_recovery_block(payload, shape, recovery_block_counts)
                return self._blocked_tool_result(tool_call_id, name, payload)

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
        compiler_repair_required: dict[str, dict[str, Any]],
    ) -> str:
        parsed = self._parse_tool_payload(content)
        self._record_reads_for_recovery(name, args, parsed, line_range_reread_required)
        path = self._tool_path(name, args, parsed)

        if (
            ok
            and isinstance(parsed, dict)
            and parsed.get("quality_bounce")
            and path
            and name in WRITE_TOOLS
        ):
            signature = self._quality_bounce_signature(path, name, args, parsed)
            prior = compiler_repair_required.get(path)
            if prior is not None and prior.get("signature") == signature:
                prior["repair_failed"] = True
                parsed["patch_quality_unresolved"] = True
                parsed["error"] = (
                    "Patch quality needs repair: "
                    + str(
                        parsed.get("repair_instructions")
                        or parsed.get("suggested_next_action")
                        or "Craft returned the same repair notes after one retry."
                    )
                )
            else:
                compiler_repair_required[path] = {
                    "tool": name,
                    "quality_bounce": True,
                    "signature": signature,
                    "repair_instructions": parsed.get("repair_instructions", ""),
                    "craft_issues": parsed.get("craft_issues", []),
                    "suggested_next_action": parsed.get("suggested_next_action", ""),
                }
            return json.dumps(parsed, ensure_ascii=False)

        if ok:
            if name in WRITE_TOOLS and path:
                edit_fallback_required.pop(path, None)
                line_range_reread_required.pop(path, None)
                compiler_repair_required.pop(path, None)
                if self._is_python_path(path) and not _is_validation_scratch_path(path):
                    syntax_validation_required.add(path)
                if path in syntax_repair_required:
                    syntax_repair_required[path]["repair_attempted"] = True
                    syntax_repair_required[path]["awaiting_validation"] = True
                    if not _is_validation_scratch_path(path):
                        syntax_validation_required.add(path)
            return content

        if name in ("edit_file", "edit_symbol", "edit_line_range"):
            edit_failed_shapes.add(self._edit_shape_signature(name, args))

        if not isinstance(parsed, dict):
            return content

        failure_class = str(parsed.get("failure_class", ""))
        if path and failure_class in EDIT_MECHANICS_FAILURE_CLASSES:
            edit_fallback_required[path] = parsed
            parsed["recoverable"] = True
            parsed["suggested_next_tool"] = "edit_line_range"
            parsed["suggested_next_action"] = "Do not retry edit_file/edit_symbol on this path. Re-read the file and use edit_line_range or write_file."
            content = json.dumps(parsed, ensure_ascii=False)
        elif path and failure_class == "edit_mechanics_stale_line_range":
            line_range_reread_required[path] = parsed
            parsed["recoverable"] = True
            parsed["suggested_next_tool"] = "read_file"
            parsed["suggested_next_action"] = "Re-read the file, then make one corrected edit_line_range attempt or use write_file."
            content = json.dumps(parsed, ensure_ascii=False)
        elif path and failure_class == "syntax_invalid":
            state = syntax_repair_required.setdefault(path, {"failed_repairs": 0})
            state["awaiting_validation"] = False
            if name in WRITE_TOOLS:
                state["failed_repairs"] = int(state.get("failed_repairs", 0)) + 1
            parsed["suggested_next_tool"] = "edit_line_range"
            parsed["suggested_next_action"] = "Repair this file's Python syntax before any unrelated tool call."
            if int(state.get("failed_repairs", 0)) > 1:
                parsed["recoverable"] = False
                parsed["error"] = "Syntax repair failed after one repair attempt. " + str(parsed.get("error", ""))
            content = json.dumps(parsed, ensure_ascii=False)
        elif (
            path
            and failure_class == "compiler_rejected"
            and parsed.get("bounce")
            and name in {"write_file", "edit_file", "edit_line_range"}
            and self._has_precise_checker_error(parsed)
        ):
            prior = compiler_repair_required.get(path)
            if prior is not None:
                prior["repair_failed"] = True
                prior["failed_tool"] = name
                parsed["recoverable"] = False
                parsed["error"] = (
                    "Patch quality needs repair after one retry. "
                    + str(parsed.get("error", ""))
                )
            else:
                compiler_repair_required[path] = {
                    "tool": name,
                    "error": parsed.get("error", ""),
                    "suggested_next_tool": self._alternate_write_tactic(name),
                    "craft_issues": parsed.get("craft_issues", []),
                    "is_new_file": bool(parsed.get("is_new_file")),
                }
                parsed["recoverable"] = True
                parsed["suggested_next_tool"] = self._alternate_write_tactic(name)
                parsed["suggested_next_action"] = (
                    "Repair the proposed code once using the precise checker error, "
                    "then retry with a different write tactic. Do not narrate."
                )
            parsed["internal_recovery_steer"] = True
            content = json.dumps(parsed, ensure_ascii=False)

        return content

    def _update_syntax_state_from_terminal(
        self,
        *,
        args: dict[str, Any],
        loop_info: dict[str, Any] | None,
        syntax_repair_required: dict[str, dict[str, Any]],
        syntax_validation_required: set[str],
    ) -> None:
        payload = loop_info.get("_terminal_payload") if isinstance(loop_info, dict) else None
        if not isinstance(payload, dict):
            return
        command = str(payload.get("command") or args.get("command") or "")
        targets = [
            path for path in self._py_compile_targets(command)
            if not _is_validation_scratch_path(path)
        ]
        if not targets:
            return
        if payload.get("ok"):
            for path in targets:
                syntax_repair_required.pop(path, None)
                syntax_validation_required.discard(path)
            return
        for path in targets:
            prior = syntax_repair_required.get(path, {})
            failed_after_repair = bool(
                prior.get("repair_attempted") or prior.get("awaiting_validation")
            )
            syntax_repair_required[path] = {
                "error": payload.get("output", ""),
                "failed_repairs": int(prior.get("failed_repairs", 0)) + (1 if failed_after_repair else 0),
                "repair_failed": failed_after_repair,
            }
            syntax_validation_required.discard(path)

    @staticmethod
    def _record_reads_for_recovery(
        name: str,
        args: dict[str, Any],
        parsed: Any,
        line_range_reread_required: dict[str, dict[str, Any]],
    ) -> None:
        if name == "read_file":
            path = str(args.get("path") or (parsed.get("path") if isinstance(parsed, dict) else ""))
            if path:
                line_range_reread_required.pop(path, None)
        elif name == "read_files":
            paths = args.get("paths")
            if isinstance(paths, list):
                for item in paths:
                    line_range_reread_required.pop(str(item), None)

    @staticmethod
    def _syntax_repair_tool_allowed(
        name: str,
        args: dict[str, Any],
        syntax_paths: set[str],
    ) -> bool:
        if name in {"read_file", "read_file_outline"}:
            return str(args.get("path", "")) in syntax_paths
        if name == "read_files":
            paths = args.get("paths")
            return isinstance(paths, list) and any(str(path) in syntax_paths for path in paths)
        if name in WRITE_TOOLS:
            return str(args.get("path", "")) in syntax_paths
        return False

    @staticmethod
    def _compiler_repair_tool_allowed(
        name: str,
        args: dict[str, Any],
        compiler_repair_required: dict[str, dict[str, Any]],
    ) -> bool:
        path = str(args.get("path", ""))
        paths = ConversationManager._compiler_repair_paths(compiler_repair_required)
        if name in {"read_file", "read_file_outline"}:
            return path in paths
        if name == "read_files":
            requested = args.get("paths")
            return isinstance(requested, list) and any(str(item) in paths for item in requested)
        if name in WRITE_TOOLS and path in paths:
            prior_tool = str(compiler_repair_required[path].get("tool", ""))
            return name != prior_tool
        return False

    @staticmethod
    def _quality_bounce_signature(
        path: str,
        name: str,
        args: dict[str, Any],
        parsed: dict[str, Any],
    ) -> str:
        issues = parsed.get("craft_issues")
        issue_bits: list[str] = []
        if isinstance(issues, list):
            for issue in issues[:8]:
                if isinstance(issue, dict):
                    issue_bits.append(
                        "|".join(
                            str(issue.get(key, ""))
                            for key in ("line", "code", "message")
                        )
                    )
        raw = json.dumps(
            {
                "path": path,
                "repair": parsed.get("repair_instructions", ""),
                "issues": issue_bits,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _quality_bounce_instruction(parsed: dict[str, Any]) -> str:
        path = str(parsed.get("path") or "")
        repair = str(parsed.get("repair_instructions") or "").strip()
        suggested = str(parsed.get("suggested_next_action") or "").strip()
        issues = parsed.get("craft_issues")
        lines = [
            WORKER_COMPILER_REPAIR_INSTRUCTION,
            "",
            f"Path: {path}" if path else "Path: (unknown)",
        ]
        if repair:
            lines.extend(["", "Repair instructions:", repair])
        if isinstance(issues, list) and issues:
            lines.append("")
            lines.append("Craft issues:")
            for issue in issues[:8]:
                if not isinstance(issue, dict):
                    continue
                line = issue.get("line")
                code = issue.get("code", "")
                message = issue.get("message", "")
                suggestion = issue.get("suggestion", "")
                prefix = f"- line {line}: " if line else "- "
                text = f"{prefix}{code}: {message}".strip()
                if suggestion:
                    text += f" Suggestion: {suggestion}"
                lines.append(text)
        if suggested:
            lines.extend(["", f"Suggested next action: {suggested}"])
        return "\n".join(lines)

    @staticmethod
    def _alternate_write_tactic(name: str) -> str:
        if name == "write_file":
            return "edit_line_range"
        return "write_file"

    @staticmethod
    def _has_precise_checker_error(parsed: dict[str, Any]) -> bool:
        issues = parsed.get("craft_issues")
        if isinstance(issues, list) and issues:
            return True
        error = str(parsed.get("error") or "")
        return bool(re.search(r"\bLine\s+\d+:", error))

    @staticmethod
    def _py_compile_targets(command: str) -> list[str]:
        if "py_compile" not in command:
            return []
        matches = re.findall(
            r"(?<![\w.-])([A-Za-z0-9_./\\:\-]+\.py)(?![\w.-])",
            command,
        )
        targets: list[str] = []
        for match in matches:
            target = _normalize_worker_path(match)
            if target.endswith("py_compile.py"):
                continue
            targets.append(target)
        return targets

    @staticmethod
    def _normalize_py_compile_path(raw: str) -> str:
        p = raw.strip().replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        return p

    @staticmethod
    def _is_python_path(path: str) -> bool:
        return path.replace("\\\\", "/").endswith(".py")

    @staticmethod
    def _tool_path(name: str, args: dict[str, Any], parsed: Any = None) -> str:
        if isinstance(parsed, dict):
            value = parsed.get("path") or parsed.get("rel_path")
            if isinstance(value, str) and value:
                return value
        value = args.get("path")
        return str(value) if value is not None else ""

    @staticmethod
    def _edit_shape_signature(name: str, args: dict[str, Any]) -> str:
        path = str(args.get("path", ""))
        if name == "edit_file":
            raw = str(args.get("old_str", ""))
            marker = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        elif name == "edit_symbol":
            marker = "|".join(
                str(args.get(key, ""))
                for key in ("symbol_type", "class_name", "symbol_name")
            )
        elif name == "edit_line_range":
            marker = f"{args.get('start_line')}:{args.get('end_line')}"
        else:
            marker = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return json.dumps({"tool": name, "path": path, "shape": marker}, sort_keys=True)

    @staticmethod
    def _default_edit_failure_class(name: str) -> str:
        if name == "edit_symbol":
            return "edit_mechanics_symbol_not_found"
        if name == "edit_line_range":
            return "edit_mechanics_stale_line_range"
        return "edit_mechanics_old_str_not_found"

    @staticmethod
    def _parse_tool_payload(content: str) -> Any:
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _recovery_payload(
        *,
        path: str,
        failure_class: str,
        error: str,
        suggested_next_tool: str,
        suggested_next_action: str,
        recoverable: bool = True,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "path": path,
            "rel_path": path,
            "error": error,
            "failure_class": failure_class,
            "recoverable": recoverable,
            "internal_recovery_steer": True,
            "suggested_tool": suggested_next_tool,
            "suggested_next_tool": suggested_next_tool,
            "suggested_next_action": suggested_next_action,
        }

    @staticmethod
    def _record_recovery_block(
        payload: dict[str, Any],
        key: str,
        recovery_block_counts: dict[str, int],
    ) -> None:
        count = recovery_block_counts.get(key, 0) + 1
        recovery_block_counts[key] = count
        payload["repeated_blocks"] = count

    @staticmethod
    def _blocked_tool_result(tool_call_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps(payload, ensure_ascii=False)
        return {
            "id": tool_call_id,
            "result_payload": content,
            "event": ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                ok=False,
                result=content,
            ),
        }

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

    def _classify_failed_worker_dispatch(
        self,
        *,
        args: dict[str, Any],
        result: WorkerDispatchResult,
        failures: dict[str, int],
        failed_attempts: int,
    ) -> dict[str, Any]:
        """Record a failed dispatch and decide whether the planner may continue."""
        if self._is_worker_internal_error(result):
            return {"counts_as_attempt": False, "blocker_reason": "internal"}

        if not self._failed_dispatch_allows_planner_continuation(result):
            return {"counts_as_attempt": False, "blocker_reason": "failed"}

        signature = self._worker_dispatch_failure_signature(args, result)
        repeated_count = failures.get(signature, 0) + 1
        failures[signature] = repeated_count

        if repeated_count >= 2:
            return {"counts_as_attempt": True, "blocker_reason": "repeated"}

        if failed_attempts + 1 >= MAX_WORKER_REDISPATCHES_PER_USER_TURN:
            return {"counts_as_attempt": True, "blocker_reason": "limit"}

        return {"counts_as_attempt": True, "blocker_reason": ""}

    @staticmethod
    def _failed_dispatch_allows_planner_continuation(
        result: WorkerDispatchResult,
    ) -> bool:
        if result.ok or result.cancelled:
            return False
        if result.extras.get("dispatch_spec_rejected"):
            return True
        return bool(result.needs_followup or result.recoverable or result.phase_boundary)

    @staticmethod
    def _is_worker_internal_error(result: WorkerDispatchResult) -> bool:
        return bool(
            result.extras.get("worker_internal_error")
            or result.extras.get("dispatch_internal_error")
        )

    def _worker_dispatch_failure_signature(
        self,
        args: dict[str, Any],
        result: WorkerDispatchResult,
    ) -> str:
        spec = {
            "goal": str(args.get("goal", "")),
            "files": [str(item) for item in args.get("files", [])]
            if isinstance(args.get("files"), list)
            else [],
            "spec": str(args.get("spec", "")),
            "acceptance": str(args.get("acceptance", "")),
            "summary": str(args.get("summary", "")),
        }
        payload = {
            "spec": spec,
            "error": self._worker_dispatch_error_signature(result),
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _worker_dispatch_error_signature(result: WorkerDispatchResult) -> str:
        extras = result.extras or {}
        if extras.get("dispatch_spec_rejected"):
            errors = extras.get("quality_errors")
            if isinstance(errors, list):
                return "dispatch_spec_rejected:" + "|".join(str(e) for e in errors)
            return "dispatch_spec_rejected"
        if extras.get("worker_internal_error"):
            return "worker_internal_error"

        parts: list[str] = []
        if result.followup_reason:
            parts.append(f"reason:{result.followup_reason}")
        for key in ("errors", "caveats"):
            values = extras.get(key)
            if isinstance(values, list) and values:
                parts.append(
                    f"{key}:"
                    + "|".join(
                        " ".join(str(value).split())[:160] for value in values[:3]
                    )
                )
        if result.needs_followup:
            parts.append("needs_followup")
        if result.recoverable:
            parts.append("recoverable")
        if result.phase_boundary:
            parts.append("phase_boundary")
        if not parts:
            parts.append(" ".join(result.summary.split())[:240])
        return ";".join(parts)

    @staticmethod
    def _is_recoverable_phase_boundary(info: dict[str, Any] | None) -> bool:
        return bool(info and info.get("recoverable") and info.get("phase_boundary"))

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
