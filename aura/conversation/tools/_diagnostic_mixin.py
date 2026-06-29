from __future__ import annotations

from typing import Any
from aura.conversation.tools._types import ToolExecResult


class DiagnosticHandlersMixin:
    """Mixin for ToolRegistry implementing diagnostic command runner."""

    def _handle_run_diagnostic_command(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        from aura.conversation.tools.diagnostic_handler import run_diagnostic_command

        command = args.get("command", "")
        timeout = int(args.get("timeout", 30))
        cwd = str(args.get("cwd") or args.get("working_directory") or "")
        try:
            result = run_diagnostic_command(command, timeout=timeout, workspace_root=self._root, cwd=cwd)
            return ToolExecResult(ok=result["ok"], payload=result)
        except Exception:
            import sys

            exc = sys.exc_info()[1]
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(exc), "command": command})
