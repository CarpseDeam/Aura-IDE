"""Workspace-observation tool handlers."""
from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ToolExecResult


class WorkspaceHandlersMixin:
    """Handlers that summarize the active workspace."""

    def _handle_get_workspace_snapshot(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        from aura.conversation.tools.workspace_snapshot_handler import (
            gather_workspace_snapshot,
        )

        try:
            return ToolExecResult(
                ok=True,
                payload=gather_workspace_snapshot(self._root),
            )
        except Exception as exc:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": str(exc),
                    "workspace_root": str(self._root),
                },
            )


__all__ = ["WorkspaceHandlersMixin"]
