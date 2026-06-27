"""Policy decisions for research-only and hybrid research routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from aura.research.intent import ResearchIntent, classify_research_intent

ANSWER_ONLY = "answer_only"
RESEARCH_THEN_WORKER = "research_then_worker"
NO_RESEARCH = "no_research"


@dataclass(frozen=True)
class ResearchPolicyDecision:
    route: str
    intent: ResearchIntent
    reason: str
    allow_worker_dispatch: bool
    requires_research_first: bool = False
    planner_guided_worker_after_research: bool = False
    deterministically_enforced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_research_policy(text: str) -> ResearchPolicyDecision:
    """Decide whether research is needed and whether Worker may run later."""
    intent = classify_research_intent(text)
    if not intent.needs_research:
        return ResearchPolicyDecision(
            route=NO_RESEARCH,
            intent=intent,
            reason=intent.reason,
            allow_worker_dispatch=True,
        )

    if intent.is_hybrid:
        return ResearchPolicyDecision(
            route=RESEARCH_THEN_WORKER,
            intent=intent,
            reason=(
                "Planner should research before dispatching a concrete code objective"
            ),
            allow_worker_dispatch=True,
            requires_research_first=True,
            planner_guided_worker_after_research=True,
        )

    return ResearchPolicyDecision(
        route=ANSWER_ONLY,
        intent=intent,
        reason="pure external/current-information request",
        allow_worker_dispatch=False,
        requires_research_first=True,
        deterministically_enforced=True,
    )
