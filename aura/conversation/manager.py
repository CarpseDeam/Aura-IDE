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
from aura.conversation.spec_quality import validate_worker_dispatch_spec
from aura.conversation.tool_limits import (
    MAX_WORKER_REDISPATCHES_PER_USER_TURN,
    ToolLimitState,
    limit_reached_payload,
)
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.sandbox import SandboxExecutor, SandboxResult

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
        reject_all_for_turn = False
        mode = getattr(self._tools, "mode", "single")
        limits = ToolLimitState(mode=mode)
        rounds_used = 0
        worker_needs_final_report = False
        worker_phase_boundary_info: dict[str, Any] | None = None
        worker_redispatches = 0

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
            for tc in tool_calls:
                if cancel_event.is_set():
                    self._cleanup_cancelled(on_event)
                    return

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

                if name == "dispatch_to_worker":
                    result = self._handle_dispatch(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        dispatch_cb=dispatch_cb,
                    )
                    if result is not None and not result.cancelled:
                        if result.extras.get("dispatch_spec_rejected"):
                            continue
                        if result.recoverable and result.phase_boundary:
                            if worker_redispatches >= MAX_WORKER_REDISPATCHES_PER_USER_TURN:
                                self._append_redispatch_limit_message(result, on_event)
                                return
                            worker_redispatches += 1
                        else:
                            _terminal_dispatch = True
                    continue

                if name == "run_research":
                    ok = self._handle_research(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        model=model,
                        cancel_event=cancel_event,
                        temperature=temperature,
                    )
                    if ok:
                        _terminal_dispatch = True
                    continue

                if name == "run_terminal_command":
                    loop_info = self._handle_terminal_command(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        cancel_event=cancel_event,
                        mode=mode,
                    )
                    if self._is_recoverable_phase_boundary(loop_info):
                        _worker_phase_boundary_info = loop_info
                    continue

                if reject_all_for_turn and name in ("write_file", "edit_file"):
                    payload = json.dumps(
                        {"ok": False, "error": "User rejected all writes in this turn."}
                    )
                    self._history.append_tool_result(tool_call_id, payload)
                    on_event(
                        ToolResult(
                            tool_call_id=tool_call_id,
                            name=name,
                            ok=False,
                            result=payload,
                            extras={"approval": "reject_all"},
                        )
                    )
                    continue

                exec_result = self._tools.execute(
                    name=name,
                    args=args,
                    approval_cb=approval_cb,
                    reject_all=False,
                )
                if exec_result.extras.get("approval") == "reject_all":
                    reject_all_for_turn = True

                tool_msg_content = exec_result.to_tool_message_content()

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

                self._history.append_tool_result(tool_call_id, tool_msg_content)
                on_event(
                    ToolResult(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=exec_result.ok,
                        result=tool_msg_content,
                        extras=exec_result.extras,
                    )
                )

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

    @staticmethod
    def _is_recoverable_phase_boundary(info: dict[str, Any] | None) -> bool:
        return bool(info and info.get("recoverable") and info.get("phase_boundary"))

    def _append_redispatch_limit_message(
        self,
        result: WorkerDispatchResult,
        on_event: EventCallback,
    ) -> None:
        completed = "; ".join(result.completed) if result.completed else result.summary
        remaining = "; ".join(result.remaining) if result.remaining else "additional focused work"
        message = (
            "The task needs another pass. "
            f"I completed {completed or 'part of the requested work'} and still need "
            f"{remaining}. Continue?"
        )
        on_event(ContentDelta(text=message))
        full_message = {
            "role": "assistant",
            "content": message,
            "reasoning_content": None,
        }
        self._history.append_assistant(full_message)
        on_event(Done(finish_reason="stop", full_message=full_message))

    # ---- dispatch_to_worker ------------------------------------------------

    def _handle_dispatch(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: EventCallback,
        dispatch_cb: DispatchCallback | None,
    ) -> WorkerDispatchResult | None:
        req = WorkerDispatchRequest.from_dict(args)
        quality = validate_worker_dispatch_spec(req.spec, req.acceptance, goal=req.goal)
        if not quality.ok:
            error_message = (
                "Planner dispatch rejected: goal, spec, and acceptance are required before Worker runs. "
                "Missing required fields:\n"
                + "\n".join(f"- {item}" for item in quality.errors)
            )
            result = WorkerDispatchResult(
                ok=False,
                summary=error_message,
                extras={
                    "dispatch_spec_rejected": True,
                    "quality_errors": list(quality.errors),
                },
            )
            payload = json.dumps(result.to_tool_payload(), ensure_ascii=False)
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="dispatch_to_worker",
                    ok=False,
                    result=payload,
                    extras={
                        "dispatch_spec_rejected": True,
                        "summary": error_message,
                        "quality_errors": list(quality.errors),
                    },
                )
            )
            return result

        if dispatch_cb is None:
            err = (
                "dispatch_to_worker is not enabled for this manager — "
                "planner/worker mode is off."
            )
            payload = json.dumps({"ok": False, "error": err})
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="dispatch_to_worker",
                    ok=False,
                    result=payload,
                )
            )
            return

        on_event(
            WorkerDispatchRequested(
                tool_call_id=tool_call_id,
                goal=req.goal,
                files=list(req.files),
                spec=req.spec,
                acceptance=req.acceptance,
                summary=req.summary,
            )
        )
        try:
            result = dispatch_cb(tool_call_id, req)
        except Exception as exc:
            result = WorkerDispatchResult(
                ok=False,
                summary=f"dispatch failed: {type(exc).__name__}: {exc}",
                cancelled=False,
            )

        payload = json.dumps(result.to_tool_payload(), ensure_ascii=False)
        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="dispatch_to_worker",
                ok=result.ok,
                result=payload,
                extras={
                    "dispatch": True,
                    "cancelled": result.cancelled,
                    "summary": result.summary,
                    "recoverable": result.recoverable,
                    "phase_boundary": result.phase_boundary,
                    "needs_followup": result.needs_followup,
                    "followup_reason": result.followup_reason,
                },
            )
        )
        return result

    def _handle_research(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: EventCallback,
        model: ModelId,
        cancel_event: threading.Event,
        temperature: float = 0.7,
    ) -> bool:
        objective = args.get("objective") or args.get("goal") or args.get("spec") or ""
        if not objective:
            payload = json.dumps({"ok": False, "error": f"objective is required. Got args: {args}"})
            self._history.append_tool_result(tool_call_id, payload)
            on_event(ToolResult(tool_call_id=tool_call_id, name="run_research", ok=False, result=payload))
            return False
        
        # Web research loop using a sub-agent
        res_tools = ToolRegistry(self._tools.workspace_root, mode="researcher")
        res_history = History()
        res_history.set_system(
            "You are a skilled Research Sub-Agent. Your goal is to answer the objective "
            "below using web search and page fetching. Be thorough but efficient. "
            "When you have enough information, write a detailed, synthesized report "
            "answering the objective and STOP. Do not provide a generic summary; "
            "answer the specific question."
        )
        res_history.append_user_text(f"Objective: {objective}")

        final_report = "Research failed to produce a report."
        thinking: ThinkingMode = "off" # Keep researcher fast
        res_limits = ToolLimitState(mode="researcher")
        
        try:
            for _round in range(5):  # Max 5 research steps
                res_limits.begin_model_round()
                if cancel_event.is_set():
                    break
                
                full_msg = None
                for ev in hooks.trigger(
                    'generate_worker_code',
                    messages=res_history.for_api(),
                    tools=res_tools.tool_defs(),
                    model=model,
                    thinking=thinking,
                    cancel_event=cancel_event,
                    temperature=temperature,
                ):
                    # For now, we don't stream researcher sub-events to the main UI
                    # to avoid card nesting complexity.
                    if isinstance(ev, Done):
                        full_msg = ev.full_message
                    if isinstance(ev, ApiError):
                        raise Exception(f"Research API Error: {ev.message}")

                if not full_msg:
                    break
                
                res_history.append_assistant(full_msg)
                tool_calls = full_msg.get("tool_calls") or []
                
                if not tool_calls:
                    final_report = full_msg.get("content") or "Research complete (no content)."
                    break
                
                for tc in tool_calls:
                    if cancel_event.is_set():
                        break
                    tc_id = tc["id"]
                    fn = tc["function"]
                    name = fn["name"]
                    try:
                        t_args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        t_args = {}

                    allowed, limit_info = res_limits.check(name)
                    if not allowed:
                        res_history.append_tool_result(tc_id, limit_reached_payload(limit_info))
                        continue
                    res_limits.record(name)
                    
                    # Execute web tool (no approval needed for search/fetch)
                    res = res_tools.execute(name, t_args, approval_cb=lambda r: ApprovalDecision("approve"))
                    res_history.append_tool_result(tc_id, res.to_tool_message_content())

            payload = json.dumps({"ok": True, "report": final_report}, ensure_ascii=False)
            self._history.append_tool_result(tool_call_id, payload)
            on_event(ToolResult(tool_call_id=tool_call_id, name="run_research", ok=True, result=payload))
            return True

        except Exception as exc:
            payload = json.dumps({"ok": False, "error": str(exc)})
            self._history.append_tool_result(tool_call_id, payload)
            on_event(ToolResult(tool_call_id=tool_call_id, name="run_research", ok=False, result=payload))
            return False

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

    def _handle_terminal_command(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: EventCallback,
        cancel_event: threading.Event,
        mode: str,
    ) -> dict[str, Any] | None:
        command = args.get("command", "")
        if not command:
            payload = json.dumps({"ok": False, "error": "command is required"})
            self._history.append_tool_result(tool_call_id, payload)
            on_event(
                ToolResult(
                    tool_call_id=tool_call_id,
                    name="run_terminal_command",
                    ok=False,
                    result=payload,
                )
            )
            return None

        timeout = int(args.get("timeout", 120))

        # Emit ToolCallStart so the GUI can create a TerminalCard
        on_event(ToolCallStart(index=0, id=tool_call_id, name="run_terminal_command"))

        # Create sandbox executor based on current settings
        from aura.config import load_settings
        settings = load_settings()
        sandbox = SandboxExecutor(
            mode=settings.sandbox_mode,  # type: ignore[arg-type]
            workspace_root=self._tools.workspace_root,
            network_enabled=True,  # Terminal commands often need network (pip install, etc.)
        )

        # Collect output for streaming to GUI
        output_lines: list[str] = []

        def on_output_chunk(text: str) -> None:
            output_lines.append(text)
            on_event(TerminalOutput(tool_call_id=tool_call_id, text=text))

        result: SandboxResult = sandbox.run_terminal_command(
            command=command,
            timeout=timeout,
            cancel_event=cancel_event,
            on_output=on_output_chunk,
        )

        full_output = result.stdout
        ok = result.ok
        exit_code = result.exit_code

        # If Docker isn't available and mode is docker, result will have the error in stderr
        if not ok and result.stderr and "Docker is not available" in result.stderr:
            full_output = f"[SANDBOX ERROR] {result.stderr}"

        payload = json.dumps(
            {
                "ok": ok,
                "exit_code": exit_code,
                "output": full_output,
                "command": command,
            },
            ensure_ascii=False,
        )

        loop_result = self._apply_loop_detection(
            mode=mode,
            name="run_terminal_command",
            args=args,
            ok=ok,
            result_payload=payload,
        )
        payload = loop_result["content"]
        loop_info = loop_result["info"]

        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_terminal_command",
                ok=ok,
                result=payload,
            )
        )
        return loop_info

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
                    # Check if all tool calls have results in history.
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
