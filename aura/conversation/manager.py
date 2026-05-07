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
import subprocess
import sys
import threading
from typing import Any, Callable

from aura.client import (
    ApiError,
    ContentDelta,
    DeepSeekClient,
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
from aura.config import MAX_TOOL_ROUNDS, ModelId, ThinkingMode
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.history import History
from aura.conversation.tools.registry import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
    ToolRegistry,
)

EventCallback = Callable[[Event], None]


class ConversationManager:
    def __init__(
        self,
        client: DeepSeekClient,
        history: History,
        tool_registry: ToolRegistry,
    ) -> None:
        self._client = client
        self._history = history
        self._tools = tool_registry

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
    ) -> None:
        """Run the model -> tool -> model loop until the model stops calling tools.

        Caller appends the user message to history before invoking this.

        `dispatch_cb` is required when the registry is in "planner" mode (the
        only mode that exposes the `dispatch_to_worker` tool). If the tool is
        called and `dispatch_cb` is None, the call returns an error result so
        the planner can recover rather than blocking forever.
        """
        reject_all_for_turn = False

        for _round in range(MAX_TOOL_ROUNDS):
            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            full_message: dict[str, Any] | None = None
            tool_defs = self._tools.tool_defs()

            for ev in self._client.stream(
                messages=self._history.for_api(),
                tools=tool_defs,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
            ):
                on_event(ev)
                if isinstance(ev, Done):
                    full_message = ev.full_message
                if isinstance(ev, ApiError):
                    return  # surface and stop

            if cancel_event.is_set():
                if full_message is not None and (
                    full_message.get("content") or full_message.get("reasoning_content")
                ):
                    full_message.pop("tool_calls", None)
                    self._history.append_assistant(full_message)
                return

            if full_message is None:
                return

            self._history.append_assistant(full_message)

            tool_calls = full_message.get("tool_calls") or []
            if not tool_calls:
                return

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

                if name == "dispatch_to_worker":
                    self._handle_dispatch(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        dispatch_cb=dispatch_cb,
                    )
                    continue

                if name == "run_research":
                    self._handle_research(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        model=model,
                        cancel_event=cancel_event,
                    )
                    continue

                if name == "run_terminal_command":
                    self._handle_terminal_command(
                        tool_call_id=tool_call_id,
                        args=args,
                        on_event=on_event,
                        cancel_event=cancel_event,
                    )
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

        on_event(
            ApiError(
                status_code=None,
                message=f"Reached max tool rounds ({MAX_TOOL_ROUNDS}) without natural stop.",
            )
        )

    # ---- dispatch_to_worker ------------------------------------------------

    def _handle_dispatch(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: EventCallback,
        dispatch_cb: DispatchCallback | None,
    ) -> None:
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

        req = WorkerDispatchRequest.from_dict(args)
        on_event(
            WorkerDispatchRequested(
                tool_call_id=tool_call_id,
                goal=req.goal,
                files=list(req.files),
                spec=req.spec,
                acceptance=req.acceptance,
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
                },
            )
        )

    def _handle_research(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: EventCallback,
        model: ModelId,
        cancel_event: threading.Event,
    ) -> None:
        objective = args.get("objective") or args.get("goal") or args.get("spec") or ""
        if not objective:
            payload = json.dumps({"ok": False, "error": f"objective is required. Got args: {args}"})
            self._history.append_tool_result(tool_call_id, payload)
            on_event(ToolResult(tool_call_id=tool_call_id, name="run_research", ok=False, result=payload))
            return
            
        # Emit a parent ToolCallStart for run_research so the GUI shows a card
        on_event(ToolCallStart(index=0, id=tool_call_id, name="run_research"))

        try:
            research_history = History()
            research_history.set_system(
                "You are an autonomous web research agent. Use the provided web_search and web_fetch "
                "tools to gather information. Synthesize your findings into a comprehensive "
                "report answering the user's objective. Be concise but thorough."
            )
            research_history.append_user_text(f"Research Objective: {objective}")
            
            research_registry = ToolRegistry(
                workspace_root=self._tools.workspace_root,
                read_only=False,
                mode="researcher"
            )
            
            manager = ConversationManager(
                client=self._client,
                history=research_history,
                tool_registry=research_registry
            )
            
            def _research_on_event(ev: Event) -> None:
                if isinstance(ev, ToolCallStart):
                    print(f"  [Researcher] Started tool: {ev.name}")
                    on_event(ev)
                if isinstance(ev, ToolCallArgsDelta):
                    on_event(ev)
                if isinstance(ev, ToolCallEnd):
                    on_event(ev)
                if isinstance(ev, ToolResult):
                    print(f"  [Researcher] Tool result [{ev.name}]: ok={ev.ok}")
                    on_event(ev)
                if isinstance(ev, Usage):
                    on_event(ev)
                
            from aura.conversation.tools.registry import ApprovalDecision
            manager.send(
                on_event=_research_on_event,
                approval_cb=lambda req: ApprovalDecision(action="reject"),
                cancel_event=cancel_event,
                model=model,
                thinking="off",
                dispatch_cb=None
            )
            
            api_msgs = research_history.for_api()
            if api_msgs and api_msgs[-1]["role"] == "assistant":
                content = api_msgs[-1].get("content") or "Research completed but no content generated."
            else:
                content = "Research failed to generate a response."
                
            payload = json.dumps({"ok": True, "report": content}, ensure_ascii=False)
            
        except Exception as exc:
            payload = json.dumps({"ok": False, "error": f"research failed: {exc}"})
            
        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_research",
                ok=json.loads(payload).get("ok", False),
                result=payload,
            )
        )

    def _handle_terminal_command(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: EventCallback,
        cancel_event: threading.Event,
    ) -> None:
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
            return

        timeout = int(args.get("timeout", 120))

        # Emit ToolCallStart so the GUI can create a TerminalCard
        on_event(ToolCallStart(index=0, id=tool_call_id, name="run_terminal_command"))

        output_lines: list[str] = []

        try:
            popen_kwargs: dict[str, Any] = {
                "shell": True,
                "cwd": str(self._tools.workspace_root),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                # STARTUPINFO with SW_HIDE definitively suppresses any console flash
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                popen_kwargs["startupinfo"] = startupinfo

            proc = subprocess.Popen(command, **popen_kwargs)

            try:
                for line in iter(proc.stdout.readline, ""):
                    if cancel_event.is_set():
                        proc.kill()
                        proc.wait()
                        payload = json.dumps(
                            {
                                "ok": False,
                                "exit_code": -1,
                                "output": "".join(output_lines),
                                "command": command,
                                "error": "Cancelled.",
                            }
                        )
                        self._history.append_tool_result(tool_call_id, payload)
                        on_event(
                            ToolResult(
                                tool_call_id=tool_call_id,
                                name="run_terminal_command",
                                ok=False,
                                result=payload,
                                extras={"cancelled": True},
                            )
                        )
                        return
                    output_lines.append(line)
                    on_event(TerminalOutput(tool_call_id=tool_call_id, text=line))

                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                output_lines.append("\n[ERROR: Command timed out after {} seconds]\n".format(timeout))

            exit_code = proc.returncode
        except Exception as exc:
            exit_code = -1
            output_lines.append(f"\n[ERROR: {type(exc).__name__}: {exc}]\n")

        full_output = "".join(output_lines)
        ok = exit_code == 0
        payload = json.dumps(
            {
                "ok": ok,
                "exit_code": exit_code,
                "output": full_output,
                "command": command,
            },
            ensure_ascii=False,
        )

        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_terminal_command",
                ok=ok,
                result=payload,
            )
        )

    def _cleanup_cancelled(self, on_event: EventCallback) -> None:
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
