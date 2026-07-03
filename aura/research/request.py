"""Canonical request object for web research."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from aura.research.adapter import WEB_RESEARCH_DRONE_ID
from aura.research.intent import ResearchIntent, classify_research_intent
from aura.research.ui_contract import RESEARCH_UI_MODE_SILENT


@dataclass(frozen=True)
class ResearchRequest:
    question: str
    original_text: str
    drone_id: str = WEB_RESEARCH_DRONE_ID
    intent_category: str = "general"
    route: str = "answer_only"
    ui_mode: str = RESEARCH_UI_MODE_SILENT
    headless: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_research_request(
    user_text: str,
    *,
    intent: ResearchIntent | None = None,
    route: str = "answer_only",
) -> ResearchRequest:
    """Build the canonical research request while preserving the user question."""
    question = " ".join(str(user_text or "").strip().split())
    resolved_intent = intent or classify_research_intent(question)
    return ResearchRequest(
        question=question,
        original_text=str(user_text or ""),
        intent_category=resolved_intent.category or "general",
        route=route,
        metadata={
            "needs_research": resolved_intent.needs_research,
            "confidence": resolved_intent.confidence,
            "reason": resolved_intent.reason,
        },
    )
