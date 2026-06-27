"""Pure deterministic research planning — no I/O, no side effects."""

from __future__ import annotations

from dataclasses import dataclass

from aura.research.models import Source
from aura.research.strategy import ResearchStrategy


@dataclass(frozen=True)
class ResearchStep:
    """A single deterministic research step."""

    kind: str  # "search" or "open"
    payload: str


def plan_search(query: str) -> ResearchStep:
    """Return a single search step for the given query."""
    return ResearchStep(kind="search", payload=query)


def plan_queries(strategy: ResearchStrategy) -> list[str]:
    """Return query variants respecting max_searches budget.

    If the strategy has explicit query_variants, returns up to
    max_searches of them.  Otherwise returns just the objective.
    """
    if strategy.query_variants:
        budget = strategy.max_searches if strategy.max_searches is not None else len(strategy.query_variants)
        return strategy.query_variants[:budget]
    return [strategy.objective]


def plan_opens(sources: list[Source], max_pages: int) -> list[ResearchStep]:
    """Return one open step per http(s) source up to max_pages.

    Drops any source whose URL does not start with ``http://`` or
    ``https://``.
    """
    steps: list[ResearchStep] = []
    count = 0
    for s in sources:
        if count >= max_pages:
            break
        if s.url.startswith(("http://", "https://")):
            steps.append(ResearchStep(kind="open", payload=s.url))
            count += 1
    return steps
