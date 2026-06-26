"""Self-contained research package with Playwright MCP integration."""

from aura.research.models import Evidence, ResearchRequest, ResearchResult, Source
from aura.research.service import research_current_info

__all__ = [
    "research_current_info",
    "ResearchResult",
    "Source",
    "Evidence",
    "ResearchRequest",
]
