"""Thin terminal-tool facade for the conversation manager."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from aura.client import TerminalCommandStarted, TerminalOutput, ToolResult
from aura.conversation.command_normalizer import normalize_command
from aura.conversation.history import History
from aura.conversation.shell_tool import ShellTool
from aura.conversation.validation_orchestrator import ValidationCommandSpec
from aura.sandbox import SandboxExecutor, WatchResult
from aura.shell.powershell_session import PowerShellSession


class ToolRunner:
    """Delegate persistent terminal work and bounded launch watching."""

    def __init__(
        self,
        history: History,
        workspace_root: Path,
        *,
        session_factory: Callable[[Path], PowerShellSession] = PowerShellSession,
    ) -> None:
        self._history = history
        self._workspace_root = workspace_root
        self._shell_tool = ShellTool(
            history,
            workspace_root,
            session_factory=session_factory,
        )

    @property
    def shell_tool(self) -> ShellTool:
        return self._shell_tool

    def set_workspace_root(self, root: Path) -> None:
        self._workspace_root = root
        self._shell_tool.set_workspace_root(root)

    def reset(self) -> None:
        self._shell_tool.reset()

    def close(self) -> None:
        self._shell_tool.close()

    def handle_terminal_command(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        cancel_event: threading.Event,
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
    ) -> dict[str, Any] | None:
        """Preserve the manager-facing API while delegating all shell policy."""
        return self._shell_tool.handle_terminal_command(
            tool_call_id=tool_call_id,
            args=args,
            on_event=on_event,
            cancel_event=cancel_event,
            explicit_validation_commands=explicit_validation_commands,
        )

    def handle_run_and_watch(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        on_event: Any,
        cancel_event: threading.Event,
        declared_run_command: str,
    ) -> dict[str, Any]:
        """Run the separate bounded-process capability on SandboxExecutor."""
        if not declared_run_command or not declared_run_command.strip():
            payload_dict = {
                "ok": False,
                "failure_class": None,
                "error": "no run command declared for this task",
                "command": "",
            }
            return self._emit_watch_result(tool_call_id, on_event, payload_dict)

        window_seconds = 10
        raw_window = args.get("window_seconds")
        if raw_window is not None:
            try:
                window_seconds = max(1, min(int(raw_window), 60))
            except (TypeError, ValueError):
                pass

        normalized = normalize_command(declared_run_command, self._workspace_root)
        declared_run_command = normalized.command
        if not normalized.valid:
            payload_dict = {
                "ok": False,
                "failure_class": "validation_command_unrunnable",
                "error": normalized.validation_error,
                "command": declared_run_command,
            }
            return self._emit_watch_result(tool_call_id, on_event, payload_dict)

        sandbox = SandboxExecutor(
            mode="host",
            workspace_root=self._workspace_root,
            network_enabled=True,
        )

        def on_output_chunk(text: str) -> None:
            on_event(TerminalOutput(tool_call_id=tool_call_id, text=text))

        def on_started() -> None:
            on_event(
                TerminalCommandStarted(
                    tool_call_id=tool_call_id,
                    command=declared_run_command,
                    cwd=str(self._workspace_root),
                )
            )

        result: WatchResult = sandbox.run_and_watch(
            command=declared_run_command,
            window_seconds=window_seconds,
            cancel_event=cancel_event,
            on_output=on_output_chunk,
            on_started=on_started,
        )
        ok = result.ok and result.exited_early
        if ok:
            failure_class = None
        elif result.cancelled:
            failure_class = "cancelled"
        elif result.failure_class == "execution_failed":
            failure_class = "launch_command_execution_failed"
        elif result.survived_window and not result.error_detected:
            failure_class = "launch_command_did_not_exit"
        elif result.error_detected:
            failure_class = "launch_command_crashed"
        elif result.exit_code is not None and result.exit_code != 0:
            failure_class = "launch_command_nonzero_exit"
        else:
            failure_class = "launch_command_failed"

        payload_dict = {
            "ok": ok,
            "failure_class": failure_class,
            "survived_window": result.survived_window,
            "exited_early": result.exited_early,
            "error_detected": result.error_detected,
            "exit_code": result.exit_code,
            "output": result.output,
            "command": declared_run_command,
            "cancelled": result.cancelled,
        }
        return self._emit_watch_result(tool_call_id, on_event, payload_dict, ok=ok)

    def _emit_watch_result(
        self,
        tool_call_id: str,
        on_event: Any,
        payload_dict: dict[str, Any],
        *,
        ok: bool | None = None,
    ) -> dict[str, Any]:
        payload = json.dumps(payload_dict, ensure_ascii=False)
        self._history.append_tool_result(tool_call_id, payload)
        on_event(
            ToolResult(
                tool_call_id=tool_call_id,
                name="run_and_watch",
                ok=bool(payload_dict.get("ok")) if ok is None else ok,
                result=payload,
            )
        )
        return {"_terminal_payload": payload_dict}


__all__ = ["ToolRunner"]
