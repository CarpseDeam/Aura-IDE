"""Self-contained research package with Playwright web-research integration."""

from aura.research.models import Evidence, ResearchRequest, ResearchResult, Source
from aura.research.service import research_current_info
from aura.research.strategy import ResearchStrategy, parse_strategy

__all__ = [
    "research_current_info",
    "ResearchResult",
    "Source",
    "Evidence",
    "ResearchRequest",
    "ResearchStrategy",
    "parse_strategy",
]
