"""Handlers for the production exit tools.

``report_blocker`` is the exit hatch for an attempt that cannot be carried out.
``report_already_satisfied`` is the exit hatch for an attempt the repository
already satisfies: the model inspected authoritative repository evidence and
records, explicitly, that the requested state already exists and no change is
required.

Both are ordinary tools on the stable production catalog, so their handlers
resolve through the ordinary executor path — approvals, receipts, history
pairing — exactly like any other tool.

Both perform no mutation of any kind: they read nothing, write nothing, and
touch no workspace state.  All each does is turn the model's stated outcome
into a structured result the send loop can recognise.
"""
from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult


class BlockerHandlersMixin:
    """Provides ``report_blocker`` / ``report_already_satisfied``, the
    production clean exits."""

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

    def _handle_report_already_satisfied(
        self,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        """Turn "the requested state already exists" into structured evidence.

        The call records the model's inspection conclusion.  It performs no
        mutation; the result is fail-closed (``mutation: False``,
        ``applied: False``) so it can never be read as an applied change, and
        the send loop treats only the structured ``already_satisfied_reported``
        flag as evidence — never assistant prose and never the absence of a
        write.
        """
        evidence = str(args.get("evidence") or "").strip()
        raw_files = args.get("target_files")
        target_files: list[str] = []
        if isinstance(raw_files, list):
            for entry in raw_files:
                text = str(entry).strip()
                if text:
                    target_files.append(text)

        payload: dict[str, Any] = {
            "ok": True,
            "already_satisfied_reported": True,
            "mutation": False,
            "applied": False,
            "evidence": evidence,
        }
        if target_files:
            payload["target_files"] = target_files
        return ToolExecResult(
            ok=True, payload=payload, extras={"already_satisfied_reported": True}
        )


__all__ = ["BlockerHandlersMixin"]
