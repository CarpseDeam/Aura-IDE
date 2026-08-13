"""Handler for ``review_implementation_plan`` — the Plan Review human pause.

Plan Review pauses the tool loop for a human decision, then returns the
approved (possibly user-edited) plan as an ordinary tool result, so the same
model resumes the same turn through the normal history/tool-result path. No
synthetic second user message, no second conversation.

The actual execution-thread <-> GUI synchronization lives behind a
``PlanReviewProxy``-shaped object the registry is wired to via
``set_plan_review_proxy`` (see ``aura/bridge/plan_review_proxy.py``). Without
one connected — e.g. a headless registry in a test — the call fails closed
and deterministically rather than blocking forever.
"""
from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult


class PlanReviewHandlersMixin:
    """Provides ``review_implementation_plan``."""

    def _handle_review_implementation_plan(
        self,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        goal = str(args.get("goal") or "").strip()
        spec = str(args.get("spec") or "").strip()
        acceptance = str(args.get("acceptance") or "").strip()
        summary = str(args.get("summary") or "").strip()
        raw_files = args.get("files")
        files: list[str] = []
        if isinstance(raw_files, list):
            for entry in raw_files:
                text = str(entry).strip()
                if text:
                    files.append(text)

        missing = [
            field_name
            for field_name, value in (("goal", goal), ("spec", spec), ("acceptance", acceptance))
            if not value
        ]
        if missing:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "approved": False,
                    "error": (
                        "review_implementation_plan requires non-empty "
                        f"{', '.join(missing)}."
                    ),
                    "failure_class": "invalid_arguments",
                },
            )

        proxy = getattr(self, "_plan_review_proxy", None)
        if proxy is None:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "approved": False,
                    "failure_class": "plan_review_unavailable",
                    "message": "No plan review surface is connected for this session.",
                },
            )

        decision = proxy.request_review(goal, files, spec, acceptance, summary)

        if not decision.approved or decision.plan is None:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "approved": False,
                    "failure_class": "plan_review_cancelled",
                },
            )

        self._plan_review.approve(decision.plan)
        payload: dict[str, Any] = {
            "ok": True,
            "approved": True,
            "user_edited": decision.user_edited,
        }
        payload.update(decision.plan.as_dict())
        return ToolExecResult(ok=True, payload=payload)


__all__ = ["PlanReviewHandlersMixin"]
