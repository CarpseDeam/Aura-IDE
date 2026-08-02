"""Handler for ``report_blocker`` — the focused action turn's only exit hatch.

The tool is not in any catalog; it reaches the model only through
:meth:`aura.conversation.tools.catalog.ToolCatalog.build_focused_action_tool_defs`.
The handler stays registered like every other handler so a replayed historical
call still resolves, and so the ordinary executor path — approvals, receipts,
history pairing — runs unchanged.

It performs no mutation of any kind: it reads nothing, writes nothing, and
touches no workspace state.  All it does is turn the model's stated blocker
into a structured result the send loop can recognise.
"""
from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult

class BlockerHandlersMixin:
    """Provides ``report_blocker``, the focused action turn's clean exit."""

    def _handle_report_blocker(
        self,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        blocker = str(args.get("blocker") or "").strip()
        if not blocker:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "report_blocker requires a non-empty 'blocker'.",
                    "failure_class": "invalid_arguments",
                },
            )

        needed = str(args.get("needed") or "").strip()
        raw_files = args.get("target_files")
        target_files: list[str] = []
        if isinstance(raw_files, list):
            for entry in raw_files:
                text = str(entry).strip()
                if text:
                    target_files.append(text)

        payload: dict[str, Any] = {
            "ok": True,
            "blocker_reported": True,
            "mutation": False,
            "applied": False,
            "blocker": blocker,
        }
        if needed:
            payload["needed"] = needed
        if target_files:
            payload["target_files"] = target_files
        return ToolExecResult(ok=True, payload=payload, extras={"blocker_reported": True})


__all__ = ["BlockerHandlersMixin"]
