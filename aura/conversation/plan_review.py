"""Plan Review — typed values and per-turn state for the human-review pause.

Plan Review is a human interaction inside the existing production tool loop,
not a second architecture: one turn still means one History, one
ConversationManager, one model. This module owns only the values that cross
the worker-thread/GUI boundary (:class:`ApprovedPlan`, :class:`PlanReviewDecision`)
and the small per-turn flag set that says whether review is required and
whether it has been satisfied yet (:class:`PlanReviewState`).

No counters, no stages, no task classifier, no general agent-state machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ApprovedPlan:
    """The final plan values a human approved, including any edits they made."""

    goal: str
    files: tuple[str, ...] = ()
    spec: str = ""
    acceptance: str = ""
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "files": list(self.files),
            "spec": self.spec,
            "acceptance": self.acceptance,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class PlanReviewDecision:
    """What the GUI resolved a pending review to.

    ``plan`` is the final (possibly user-edited) plan when ``approved`` is
    True, and is always None on cancellation.
    """

    approved: bool
    plan: ApprovedPlan | None = None
    user_edited: bool = False


class PlanReviewState:
    """Owns whether Plan Review is required/approved for the active turn.

    Frozen once per real user turn (see ``ConversationBridge.send()``), so a
    toolbar toggle flipped mid-turn never mutates the turn already in flight.
    """

    def __init__(self) -> None:
        self._required: bool = False
        self._approved: bool = False
        self._approved_plan: ApprovedPlan | None = None

    def begin_turn(self, *, required: bool) -> None:
        """Reset for a new real user turn."""
        self._required = required
        self._approved = False
        self._approved_plan = None

    def approve(self, plan: ApprovedPlan) -> None:
        self._approved = True
        self._approved_plan = plan

    @property
    def required(self) -> bool:
        return self._required

    @property
    def approved(self) -> bool:
        return self._approved

    @property
    def approved_plan(self) -> ApprovedPlan | None:
        return self._approved_plan

    @property
    def mutation_blocked(self) -> bool:
        """Whether a MUTATION-effect tool call must be refused right now."""
        return self._required and not self._approved


__all__ = ["ApprovedPlan", "PlanReviewDecision", "PlanReviewState"]
