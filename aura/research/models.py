"""Frozen dataclasses for research data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Source:
    """A single search result source."""

    url: str
    title: str
    snippet: str = ""


@dataclass(frozen=True)
class Evidence:
    """Textual evidence extracted from a source."""

    source: Source
    text: str
    fetched_at: str  # ISO-8601 UTC string


@dataclass(frozen=True)
class ResearchRequest:
    """A request to perform research."""

    query: str
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchResult:
    """The complete result of a research query."""

    query: str
    sources: list[Source] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    ok: bool = True
    notes: list[str] = field(default_factory=list)
