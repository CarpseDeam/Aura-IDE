"""ResearchStrategy — normalised strategy from free-form constraints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResearchStrategy:
    """Normalised research strategy parsed from free-form constraints."""

    objective: str
    freshness: str | None = None
    source_goal: str | None = None
    answer_shape: str | None = None
    query_variants: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    max_searches: int | None = None
    max_search_results: int | None = None
    max_pages_to_open: int | None = None
    max_evidence_chars: int | None = None


def parse_strategy(query: str, constraints: dict[str, Any] | None) -> ResearchStrategy:
    """Build a ResearchStrategy from query + constraints.

    Falls back safely on bad values (non-int int fields, non-list
    list fields, etc.).  Handles backward-compatible *max_pages* ->
    *max_search_results* + *max_pages_to_open* mapping.
    """
    kwargs: dict[str, Any] = {"objective": query}

    if constraints is None:
        return ResearchStrategy(**kwargs)

    # scalar strings: freshness, source_goal, answer_shape
    for key in ("freshness", "source_goal", "answer_shape"):
        val = constraints.get(key)
        if val is not None and isinstance(val, str):
            kwargs[key] = val

    # lists: query_variants, allowed_domains, blocked_domains, avoid
    for key in ("query_variants", "allowed_domains", "blocked_domains", "avoid"):
        val = constraints.get(key)
        if val is not None and isinstance(val, list):
            kwargs[key] = [str(v) for v in val]

    # ints: max_searches, max_search_results, max_pages_to_open, max_evidence_chars
    for key in ("max_searches", "max_search_results", "max_pages_to_open", "max_evidence_chars"):
        val = constraints.get(key)
        if val is not None:
            try:
                kwargs[key] = int(val)
            except (ValueError, TypeError):
                pass

    # backward compat: old max_pages -> both new fields only when absent
    old_max_pages = constraints.get("max_pages")
    if old_max_pages is not None:
        try:
            old_val = int(old_max_pages)
            kwargs.setdefault("max_search_results", old_val)
            kwargs.setdefault("max_pages_to_open", old_val)
        except (ValueError, TypeError):
            pass

    return ResearchStrategy(**kwargs)
