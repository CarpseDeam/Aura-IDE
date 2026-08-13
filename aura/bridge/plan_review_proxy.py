"""Qt thread-crossing proxy for one active Plan Review request.

Mirrors the synchronization shape ``_ApprovalProxy`` uses for diff approval,
scoped to exactly one thing: pausing the conversation thread for a human plan
decision, then resuming it with that decision.

    conversation/tool thread
      -> emits ``reviewRequested`` (Qt signal, thread-safe to emit)
      -> blocks on a ``threading.Event``
    GUI thread
      -> renders the PlanReviewCard inline in chat
      -> user edits / implements / cancels
      -> calls ``resolve_approved`` / ``resolve_cancelled``
    conversation thread resumes with the ``PlanReviewDecision``

Normal production runs one turn at a time, so one pending slot is enough —
this deliberately remains a small, direct synchronization point.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from aura.conversation.plan_review import ApprovedPlan, PlanReviewDecision


@dataclass
class _PendingReview:
    review_id: str
    decision_event: threading.Event = field(default_factory=threading.Event)
    decision: PlanReviewDecision | None = None


class PlanReviewProxy(QObject):
    """Marshals one active Plan Review request from the conversation thread to the GUI."""

    # review_id, goal, files, spec, acceptance, summary
    reviewRequested = Signal(str, str, list, str, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending: _PendingReview | None = None

    # ---- conversation-thread side ------------------------------------------

    def request_review(
        self,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str,
    ) -> PlanReviewDecision:
        """Called from the conversation thread. Blocks until the GUI resolves it.

        No arbitrary timeout: a human review can legitimately take longer
        than an automated wait would allow. The only guaranteed unblock path
        is Stop, via :meth:`cancel_active`.
        """
        pending = _PendingReview(review_id=uuid.uuid4().hex)
        with self._lock:
            self._pending = pending
        self.reviewRequested.emit(
            pending.review_id, goal, list(files), spec, acceptance, summary
        )
        pending.decision_event.wait()
        with self._lock:
            if self._pending is pending:
                self._pending = None
        return pending.decision or PlanReviewDecision(approved=False)

    # ---- GUI-thread side ------------------------------------------------

    def resolve_approved(
        self, review_id: str, plan: ApprovedPlan, *, user_edited: bool
    ) -> bool:
        return self._resolve(
            review_id,
            PlanReviewDecision(approved=True, plan=plan, user_edited=user_edited),
        )

    def resolve_cancelled(self, review_id: str) -> bool:
        return self._resolve(review_id, PlanReviewDecision(approved=False))

    def cancel_active(self) -> None:
        """Unblock a pending review immediately (Stop action)."""
        with self._lock:
            pending = self._pending
        if pending is not None:
            self._resolve(pending.review_id, PlanReviewDecision(approved=False))

    def has_active_review(self, review_id: str) -> bool:
        with self._lock:
            return self._pending is not None and self._pending.review_id == review_id

    def _resolve(self, review_id: str, decision: PlanReviewDecision) -> bool:
        with self._lock:
            pending = self._pending
            if pending is None or pending.review_id != review_id:
                return False
            pending.decision = decision
        pending.decision_event.set()
        return True


__all__ = ["PlanReviewProxy"]
