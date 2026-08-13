"""Task Checklist tool handler."""

from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult
from aura.task_checklist import parse_task_checklist_snapshot


class TaskChecklistHandlersMixin:
    """Static handler for the display-only Task Checklist tool."""

    def _handle_update_task_checklist(
        self,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        snapshot, errors = parse_task_checklist_snapshot(args)
        if snapshot is None or errors:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "Invalid Task Checklist snapshot.",
                    "errors": errors,
                },
                extras={"task_checklist": True},
            )
        payload = {"ok": True, **snapshot.to_dict()}
        return ToolExecResult(
            ok=True,
            payload=payload,
            extras={"task_checklist": True},
        )
