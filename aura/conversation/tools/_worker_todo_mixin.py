"""Worker TODO tool handler."""

from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult
from aura.worker_todo import parse_worker_todo_snapshot


class WorkerTodoHandlersMixin:
    """Static handler for the display-only Worker TODO tool."""

    def _handle_update_worker_todo(
        self,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        snapshot, errors = parse_worker_todo_snapshot(args)
        if snapshot is None or errors:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "Invalid Worker TODO snapshot.",
                    "errors": errors,
                },
                extras={"worker_todo": True},
            )
        payload = {"ok": True, **snapshot.to_dict()}
        return ToolExecResult(
            ok=True,
            payload=payload,
            extras={"worker_todo": True},
        )
