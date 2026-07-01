"""Deterministic intent checks for external web research."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ResearchIntent:
    needs_research: bool
    category: str
    reason: str
    confidence: float
    is_hybrid: bool = False
    is_local: bool = False
    matched_terms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_LOCAL_PATTERNS: tuple[str, ...] = (
    r"\b(?:this|the)\s+(?:repo|repository|workspace|project|codebase|file|folder|directory)\b",
    r"\b(?:repo|repository|workspace|codebase)\s+(?:inspection|search|files?|tree)\b",
    r"\b(?:read|inspect|open|search|grep|scan|summarize)\s+(?:the\s+)?(?:local\s+)?(?:file|repo|repository|workspace|codebase)\b",
    r"\b(?:git\s+status|git\s+diff|git\s+log|current\s+branch|latest\s+snapshot|last\s+commit)\b",
    r"\b(?:in|inside|from)\s+(?:this|the)\s+(?:repo|repository|workspace|project|codebase|file)\b",
    r"\b[a-z0-9_.-]+(?:[/\\][a-z0-9_.-]+)+\.(?:py|pyw|ts|tsx|js|jsx|json|toml|yaml|yml|md|txt|css|scss|html|gd|cs|java|go|rs|cpp|c|h|hpp)\b",
)

_CODING_PATTERNS: tuple[str, ...] = (
    r"\b(?:add|build|change|clean\s+up|cleanup|create|delete|extract|fix|"
    r"implement|modify|move|refactor|remove|rename|repair|update|wire|integrate)\b",
    r"\b(?:write|edit|patch)\s+(?:code|tests?|files?)\b",
)

_RESEARCH_RULES: tuple[tuple[str, tuple[str, ...]]] = (
    (
        "latest_current",
        (
            r"\b(?:latest|current|currently|recent|newest|up[- ]?to[- ]?date|as of today|right now)\b",
            r"\b(?:today|tomorrow|tonight|this week|this month|this year|weekend)\b",
        ),
    ),
    (
        "docs_api",
        (
            r"\b(?:docs?|documentation|api reference|api docs?|examples?|sample code)\b",
            r"\b(?:how do i use|usage example)\b.*\b(?:api|sdk|library|package)\b",
        ),
    ),
    (
        "pricing",
        (
            r"\b(?:price|prices|pricing|cost|costs|rate|rates|subscription|plan)\b",
            r"\b(?:how much)\b.*\b(?:cost|price|charge)\b",
        ),
    ),
    (
        "versions_releases",
        (
            r"\b(?:version|versions|release|releases|released|changelog|change log|release notes|breaking changes)\b",
        ),
    ),
    (
        "schedule",
        (
            r"\b(?:schedule|schedules|fixture|fixtures|match|matches|game|games|score|scores)\b",
            r"\b(?:who|when|what time)\b.*\b(?:play|plays|playing|played|starts?|kickoff|tipoff)\b",
            r"\b(?:next|upcoming)\s+(?:match|game|fixture|event|release)\b",
        ),
    ),
    (
        "current_role",
        (
            r"\b(?:who is|who's)\s+(?:the\s+)?(?:current\s+)?(?:ceo|cto|cfo|president|prime minister|mayor|governor|chair|leader|director)\b",
            r"\b(?:current\s+)?(?:ceo|cto|cfo|president|prime minister|mayor|governor|chair|leader|director)\s+(?:of|for)\b",
        ),
    ),
    (
        "error_lookup",
        (
            r"\b(?:look up|lookup|search|google|research)\b.*\b(?:error|exception|stack trace|traceback|status code|errno)\b",
            r"\b(?:what does|what is)\b.*\b(?:error|exception|status code|errno)\b.*\b(?:mean|indicate)\b",
            r"\b(?:err_[a-z0-9_]+|e[a-z]+[0-9][a-z0-9_]*|enoent|eacces|eperm|eexist|enotdir|eisdir|http\s*[45]\d\d)\b",
        ),
    ),
    (
        "external_reference",
        (
            r"https?://\S+",
            r"\b(?:external|online|internet|web|website|source|reference|article|paper|manual)\b",
            r"\b(?:look up|lookup|research|search for|find online|check online|verify online)\b",
        ),
    ),
)

_EXTERNAL_RESEARCH_TERMS: tuple[str, ...] = (
    "docs",
    "documentation",
    "api",
    "pricing",
    "version",
    "release",
    "changelog",
    "online",
    "web",
    "url",
    "http",
    "external",
)


def classify_research_intent(text: str) -> ResearchIntent:
    """Classify whether a request needs external/current information."""
    raw = str(text or "").strip()
    normalized = _normalize(raw)
    if not normalized:
        return ResearchIntent(False, "none", "empty request", 0.4)

    local_terms = _matches_any(normalized, _LOCAL_PATTERNS)
    coding = _looks_like_coding(normalized)
    category, terms = _research_category(normalized)
    has_research = bool(category)

    if local_terms and not _has_explicit_external_marker(normalized):
        return ResearchIntent(
            False,
            "local_workspace",
            "request points at local repo/workspace context",
            0.9,
            is_local=True,
            matched_terms=tuple(local_terms),
        )

    if not has_research:
        reason = "ordinary coding request" if coding else "no external research trigger"
        return ResearchIntent(False, "none", reason, 0.75)

    if coding:
        return ResearchIntent(
            True,
            category,
            "coding task explicitly depends on external/current facts",
            0.86,
            is_hybrid=True,
            matched_terms=tuple(terms),
        )

    return ResearchIntent(
        True,
        category,
        "request asks for external/current information",
        0.88,
        matched_terms=tuple(terms),
    )


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _research_category(normalized: str) -> tuple[str, list[str]]:
    for category, patterns in _RESEARCH_RULES:
        terms = _matches_any(normalized, patterns)
        if terms:
            return category, terms
    return "", []


def _matches_any(normalized: str, patterns: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            matches.append(match.group(0))
    return matches


def _looks_like_coding(normalized: str) -> bool:
    return bool(_matches_any(normalized, _CODING_PATTERNS))


def _has_explicit_external_marker(normalized: str) -> bool:
    compact = normalized.replace("-", " ")
    return any(term in compact for term in _EXTERNAL_RESEARCH_TERMS)
