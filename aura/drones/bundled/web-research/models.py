"""Shared data models and constants for the Web Research Drone."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


TIMEZONES = "GMT|UTC|ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT"
TEAM_ALIASES = {
    "ENG": "England",
    "USA": "USA",
}

STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "and",
    "answer",
    "are",
    "because",
    "been",
    "before",
    "being",
    "can",
    "could",
    "current",
    "did",
    "does",
    "doing",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "is",
    "it",
    "its",
    "latest",
    "more",
    "most",
    "new",
    "now",
    "of",
    "on",
    "or",
    "please",
    "recent",
    "show",
    "tell",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "this",
    "today",
    "tomorrow",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


@dataclass(frozen=True)
class SourceTarget:
    url: str
    title: str = ""
    kind: str = "web"


@dataclass
class FetchedSource:
    target: SourceTarget
    title: str
    text: str
    fetched_at: str
    ok: bool
    error: str = ""
    excerpt: str = ""
    route: str = "http"
    links: list[SourceTarget] = field(default_factory=list)
    final_url: str = ""


@dataclass
class ExtractedAnswer:
    answer: str
    verified_facts: list[str]
    evidence: list[dict[str, Any]]
    gaps: list[str]
    confidence: str


@dataclass(frozen=True)
class ResearchQuestion:
    raw: str
    normalized: str
    tags: list[str]
    terms: list[str]
    search_queries: list[str]


@dataclass(frozen=True)
class EvidenceChunk:
    source_url: str
    title: str
    text: str
    excerpt: str
    score: float
    chunk_index: int
