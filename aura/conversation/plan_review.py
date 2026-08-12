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

from aura.conversation.tools.effects import ToolEffect

#: The effect classes Plan Review refuses while required and not yet
#: approved. This is the single authoritative statement of that policy —
#: every execution seam (``ToolRegistry.execute`` and the tool round's
#: terminal-special-cased dispatch) calls :meth:`PlanReviewState.blocks`
#: rather than repeating this set.
_BLOCKED_EFFECTS = frozenset({ToolEffect.MUTATION, ToolEffect.COMMAND})


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

    def blocks(self, effect: ToolEffect) -> bool:
        """Whether Plan Review must refuse a call with this effect right now.

        True only while review is required and not yet approved, and only for
        the consequential effects in :data:`_BLOCKED_EFFECTS` (MUTATION and
        COMMAND). OBSERVATION and BOOKKEEPING — including
        ``review_implementation_plan`` itself — are never blocked.
        """
        return self._required and not self._approved and effect in _BLOCKED_EFFECTS


def blocked_tool_payload() -> dict[str, Any]:
    """The one deterministic refusal payload for a Plan-Review-blocked call.

    Shared by every execution seam that calls :meth:`PlanReviewState.blocks`
    so a blocked call always reports the same ``failure_class`` and
    ``required_tool``, regardless of which seam refused it.
    """
    return {
        "ok": False,
        "failure_class": "plan_review_required",
        "required_tool": "review_implementation_plan",
        "message": (
            "Plan Review is enabled. Review and approve the "
            "implementation plan before workspace mutations."
        ),
    }


__all__ = [
    "ApprovedPlan",
    "PlanReviewDecision",
    "PlanReviewState",
    "blocked_tool_payload",
]
