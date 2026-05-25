"""ToolRunner — delegates tool execution for ConversationManager."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from aura.client import (
    ApiError,
    Done,
    TerminalOutput,
    ToolCallStart,
    ToolResult,
    WorkerDispatchRequested,
)
from aura.config import ModelId, ThinkingMode, load_settings, redact_secrets
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.spec_quality import validate_worker_dispatch_spec
from aura.conversation.tool_limits import (
    ToolLimitState,
    limit_reached_payload,
)
from aura.conversation.terminal_policy import worker_terminal_command_allowed
from aura.conversation.tools._types import (
    ApprovalDecision,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.hooks import hooks
from aura.sandbox import SandboxExecutor, SandboxResult

DEFAULT_TERMINAL_TIMEOUT_SECONDS = 45
DEFAULT_PY_COMPILE_TIMEOUT_SECONDS = 30
MAX_TERMINAL_TIMEOUT_SECONDS = 300


class ToolRunner:
    """Owns the execution of dispatch, research, and terminal tools."""

    def __init__(
        self, history: History, workspace_root: Path, loop_detector: LoopDetector
    ) -> None:
        self._history = history
        self._workspace_root = workspace_root
        self._loop_detector = loop_detector

    # ------------------------------------------------------------------
    # dispatch_to_worker
    # ------------------------------------------------------------------

    def handle_dispatch(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        dispatch_cb: DispatchCallback | None,
    ) -> WorkerDispatchResult | None:
        req = WorkerDispatchRequest.from_dict(args)
        quality = validate_worker_dispatch_spec(req.spec, req.acceptance, goal=req.goal)
        if not quality.ok:
            missing = [
                item.removesuffix(" is required") for item in quality.errors
            ]
            missing_text = ", ".join(missing) if missing else "required details"
            error_message = (
                f"Plan incomplete — missing {missing_text}. "
                "The Worker was not started. Missing required fields:\n"
                + "\n".join(f"- {item}" for item in quality.errors)
            )
            result = WorkerDispatchResult(
                ok=False,
                summary=error_message,
                recoverable=True,
                extras={
                    "dispatch_not_started": True,
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
                        "dispatch_not_started": True,
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
                summary="Harness error due to an internal Worker dispatch exception.",
                cancelled=False,
                recoverable=False,
                extras={
                    "worker_internal_error": True,
                    "error_type": type(exc).__name__,
                    "internal_error": redact_secrets(f"{type(exc).__name__}: {exc}"),
                },
            )

        payload = json.dumps(result.to_tool_payload(), ensure_ascii=False)
        self._history.append_tool_result(tool_call_id, payload)
        event_extras = {
            "dispatch": True,
            "cancelled": result.cancelled,
            "summary": result.summary,
            "recoverable": result.recoverable,
            "phase_boundary": result.phase_boundary,
            "needs_followup": result.needs_followup,
            "followup_reason": result.followup_reason,
        }
        event_extras.update(result.extras)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="dispatch_to_worker",
                ok=result.ok,
                result=payload,
                extras=event_extras,
            )
        )
        return result

    # ------------------------------------------------------------------
    # run_research
    # ------------------------------------------------------------------

    def handle_research(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
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
        res_tools = ToolRegistry(self._workspace_root, mode="researcher")
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
        thinking: ThinkingMode = "off"  # Keep researcher fast
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

    # ------------------------------------------------------------------
    # run_terminal_command
    # ------------------------------------------------------------------

    def handle_terminal_command(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        cancel_event: threading.Event,
        mode: str,
        explicit_validation_commands: list[str] | None = None,
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
            return {"_terminal_payload": {"ok": False, "error": "command is required", "command": ""}}

        if mode == "worker":
            decision = worker_terminal_command_allowed(
                str(command),
                explicit_validation_commands=explicit_validation_commands,
            )
            if not decision.allowed:
                blocked_payload = decision.to_blocked_payload(str(command))
                payload = json.dumps(blocked_payload, ensure_ascii=False)
                self._history.append_tool_result(tool_call_id, payload)
                on_event(
                    ToolResult(
                        tool_call_id=tool_call_id,
                        name="run_terminal_command",
                        ok=False,
                        result=payload,
                    )
                )
                return {
                    "recoverable": True,
                    "phase_boundary": False,
                    "reason": decision.failure_class,
                    "_terminal_payload": blocked_payload,
                }

        timeout = self._resolve_terminal_timeout(
            command=command,
            timeout_arg=args.get("timeout"),
        )

        # Emit ToolCallStart so the GUI can create a TerminalCard
        on_event(ToolCallStart(index=0, id=tool_call_id, name="run_terminal_command"))

        settings = load_settings()
        sandbox = SandboxExecutor(
            mode=settings.sandbox_mode,  # type: ignore[arg-type]
            workspace_root=self._workspace_root,
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

        payload_dict = {
            "ok": ok,
            "exit_code": exit_code,
            "output": full_output,
            "command": command,
        }
        payload = json.dumps(payload_dict, ensure_ascii=False)

        observed = self._loop_detector.observe(
            mode=mode,
            tool_name="run_terminal_command",
            args=args,
            ok=ok,
            content=payload,
        )
        payload = observed.content
        loop_info = observed.info

        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_terminal_command",
                ok=ok,
                result=payload,
            )
        )
        if loop_info is None:
            loop_info = {}
        loop_info["_terminal_payload"] = payload_dict
        return loop_info

    def _resolve_terminal_timeout(
        self,
        command: str,
        timeout_arg: Any,
    ) -> int:
        """Resolve a safe timeout for terminal commands."""
        if timeout_arg is None:
            if self._is_py_compile_command(command):
                return DEFAULT_PY_COMPILE_TIMEOUT_SECONDS
            return DEFAULT_TERMINAL_TIMEOUT_SECONDS

        try:
            timeout = int(timeout_arg)
        except (TypeError, ValueError):
            if self._is_py_compile_command(command):
                return DEFAULT_PY_COMPILE_TIMEOUT_SECONDS
            return DEFAULT_TERMINAL_TIMEOUT_SECONDS

        return max(1, min(timeout, MAX_TERMINAL_TIMEOUT_SECONDS))

    def _is_py_compile_command(self, command: str) -> bool:
        normalized = " ".join(command.strip().lower().split())
        return "python -m py_compile" in normalized
