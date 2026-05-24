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
import threading
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
        _edit_failures: dict[str, list[str]] = {}
        _edit_tactic_blocked: dict[str, set[str]] = {}

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
                nonlocal _terminal_dispatch, _worker_phase_boundary_info, reject_all_for_turn, worker_redispatches
                tool_call_id = task["id"]
                name = task["name"]
                args = task["args"]

                # Worker mode tactic-forcing: block retrying same edit tactic on a file that already failed.
                if mode == "worker" and name in ("edit_file", "edit_symbol"):
                    file_path = args.get("path", "")
                    if file_path and file_path in _edit_tactic_blocked and name in _edit_tactic_blocked[file_path]:
                        payload = json.dumps({
                            "ok": False,
                            "error": (
                                "The same edit tactic (" + name + ") was attempted again on " + file_path +
                                " after a prior failure. This is not allowed. "
                                "Use a different tactic: edit_line_range or write_file."
                            )
                        })
                        return {
                            "id": tool_call_id,
                            "result_payload": payload,
                            "event": ToolResult(
                                tool_call_id=tool_call_id,
                                name=name,
                                ok=False,
                                result=payload,
                            )
                        }

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
                    if self._is_recoverable_phase_boundary(loop_info):
                        _worker_phase_boundary_info = loop_info
                    return {"id": tool_call_id, "skip": True}

                if reject_all_for_turn and name in WRITE_TOOLS:
                    payload = json.dumps(
                        {"ok": False, "error": "User rejected all writes in this turn."}
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

                # Worker mode tactic-forcing: track edit failures and inject recovery hint.
                if mode == "worker" and name in ("edit_file", "edit_symbol") and not exec_result.ok:
                    file_path = args.get("path", "")
                    if file_path:
                        _edit_tactic_blocked.setdefault(file_path, set()).add(name)
                        hint = (
                            "[edit-recovery: Your edit on " + file_path +
                            " failed because old_str/symbol was not matched. "
                            "Do NOT retry edit_file/edit_symbol on this path. "
                            "Use a different tactic: edit_line_range (if you know the line numbers from a prior read_file) "
                            "or write_file (to replace the whole file).]"
                        )
                        try:
                            payload_dict = json.loads(tool_msg_content)
                            if isinstance(payload_dict, dict):
                                payload_dict["error"] = payload_dict.get("error", "") + "\n" + hint
                                tool_msg_content = json.dumps(payload_dict)
                        except (json.JSONDecodeError, TypeError):
                            tool_msg_content = tool_msg_content + "\n" + hint

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
                    )
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

            if _worker_phase_boundary_info is not None:
                worker_phase_boundary_info = _worker_phase_boundary_info
                worker_needs_final_report = True
                continue

            # If any dispatch_to_worker or run_research completed, stop the loop.
            # The Worker Completed card is the final user-facing result.
            if _terminal_dispatch:
                return

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
                "Worker failed due to an internal error. "
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
                "Worker failed. I stopped automatic redispatch so the failure can be addressed first."
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
