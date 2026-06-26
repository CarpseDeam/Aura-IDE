"""Pure deterministic research planning — no I/O, no side effects."""

from __future__ import annotations

from dataclasses import dataclass

from aura.research.limits import ResearchLimits
from aura.research.models import Source


@dataclass(frozen=True)
class ResearchStep:
    """A single deterministic research step."""

    kind: str  # "search" or "open"
    payload: str


def plan_search(query: str) -> ResearchStep:
    """Return a single search step for the given query."""
    return ResearchStep(kind="search", payload=query)


def plan_opens(sources: list[Source], limits: ResearchLimits) -> list[ResearchStep]:
    """Return one open step per http(s) source up to limits.max_pages.

    Drops any source whose URL does not start with ``http://`` or
    ``https://``.
    """
    steps: list[ResearchStep] = []
    count = 0
    for s in sources:
        if count >= limits.max_pages:
            break
        if s.url.startswith(("http://", "https://")):
            steps.append(ResearchStep(kind="open", payload=s.url))
            count += 1
    return steps
