"""Thin terminal-tool facade for the conversation manager."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from aura.conversation.history import History
from aura.conversation.shell_tool import ShellTool
from aura.conversation.validation_orchestrator import ValidationCommandSpec
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


__all__ = ["ToolRunner"]
