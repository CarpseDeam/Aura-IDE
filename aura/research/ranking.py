"""Source deduplication and ranking against a ResearchStrategy."""

from __future__ import annotations

import urllib.parse

from aura.research.models import Source
from aura.research.strategy import ResearchStrategy


def _normalise_url(url: str) -> str:
    """Lowercase, strip trailing slash, remove www. prefix."""
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    if hostname.startswith("www."):
        hostname = hostname[4:]
    path = parsed.path.rstrip("/")
    # Reconstruct without query/fragment for dedup
    return f"{parsed.scheme}://{hostname}{path}".lower()


def deduplicate_sources(sources: list[Source]) -> list[Source]:
    """Deduplicate sources by normalised URL.

    The first occurrence of each normalised URL is kept; later
    duplicates are dropped.
    """
    seen: set[str] = set()
    result: list[Source] = []
    for s in sources:
        norm = _normalise_url(s.url)
        if norm not in seen:
            seen.add(norm)
            result.append(s)
    return result


def _score_source(source: Source, strategy: ResearchStrategy) -> float:
    """Score a single source against the strategy (higher is better).

    Base score 1.0.  Blocked domains get -10 (eliminates).
    Allowed domains get +0.3.  Avoid terms in title/snippet get
    -0.2 each (capped at -0.6).  .gov / .edu TLD gets +0.2.
    source_goal keyword overlap in title gets +0.15.
    source_goal in hostname gets +0.1.
    """
    score = 1.0

    parsed = urllib.parse.urlparse(source.url)
    hostname = (parsed.hostname or "").lower()

    # Blocked domain check (case-insensitive)
    for blocked in strategy.blocked_domains:
        b = blocked.lower()
        if hostname == b or hostname.endswith("." + b):
            score -= 10.0

    # If blocked, short-circuit — no further bonuses
    if score < 0:
        return score

    # Allowed domain check
    for allowed in strategy.allowed_domains:
        a = allowed.lower()
        if hostname == a or hostname.endswith("." + a):
            score += 0.3
            break  # only one bonus

    # Avoid terms in title / snippet
    title_lower = source.title.lower()
    snippet_lower = source.snippet.lower()
    avoid_penalty = 0.0
    for term in strategy.avoid:
        t = term.lower()
        if t in title_lower or t in snippet_lower:
            avoid_penalty += 0.2
    score -= min(avoid_penalty, 0.6)

    # .gov / .edu TLD bonus
    tld = hostname.rsplit(".", 1)[-1] if "." in hostname else ""
    if tld in ("gov", "edu"):
        score += 0.2

    # source_goal keyword overlap in title
    if strategy.source_goal:
        goal_words = set(strategy.source_goal.lower().split())
        title_words = set(title_lower.split())
        overlap = len(goal_words & title_words)
        if overlap > 0:
            score += 0.15

    # source_goal in hostname
    if strategy.source_goal and strategy.source_goal.lower() in hostname:
        score += 0.1

    return score


def rank_sources(
    sources: list[Source],
    strategy: ResearchStrategy,
) -> list[tuple[Source, float]]:
    """Score each source against the strategy and return sorted (source, score).

    Returns descending by score.  Sources with score < 0 (blocked)
    are still included in the list but will have negative scores.
    """
    scored: list[tuple[Source, float]] = []
    for s in sources:
        scored.append((s, _score_source(s, strategy)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
