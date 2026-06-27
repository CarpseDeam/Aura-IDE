"""Public research service — entry point for web research."""

from __future__ import annotations

import datetime
from typing import Any

from aura.research.limits import DEFAULT_LIMITS, Deadline, ResearchLimits
from aura.research.models import Evidence, ResearchResult, Source
from aura.research.planner import plan_opens, plan_queries, plan_search
from aura.research.playwright import PlaywrightResearcher
from aura.research.ranking import deduplicate_sources, rank_sources
from aura.research.strategy import parse_strategy

_DEFAULT_MAX_EVIDENCE_CHARS = 8000


def research_current_info(
    query: str,
    constraints: dict[str, Any] | None = None,
) -> ResearchResult:
    """Fetch current external information from the live web.

    Returns a grounded ``ResearchResult`` with sources, evidence, and
    status notes.  This function NEVER raises — all exceptions are
    caught and returned as a non-ok result.
    """
    try:
        # 1. Parse strategy from query + constraints
        strategy = parse_strategy(query, constraints)

        # 2. Build ResearchLimits for timeout (backward compat)
        timeout = DEFAULT_LIMITS.timeout_seconds
        if constraints:
            ts = constraints.get("timeout_seconds")
            if ts is not None:
                try:
                    timeout = float(ts)
                except (ValueError, TypeError):
                    pass
        limits = ResearchLimits(
            max_pages=strategy.max_pages_to_open or DEFAULT_LIMITS.max_pages,
            timeout_seconds=timeout,
        )

        # 3. Start researcher
        researcher = PlaywrightResearcher(limits=limits)
        try:
            if not researcher.start():
                return ResearchResult(
                    query=query,
                    ok=False,
                    notes=[researcher._unavailable_reason or "browser backend unavailable"],
                )

            # 4. Build query variants and search
            queries = plan_queries(strategy)
            all_sources: list[Source] = []
            search_notes: list[str] = []

            for q in queries:
                plan_search(q)  # deterministic planning step (kept for completeness)
                sources = researcher.search(q, max_results=strategy.max_search_results or 5)
                if sources:
                    all_sources.extend(sources)
                    search_notes.append(f"search \"{q}\": {len(sources)} results")
                else:
                    search_notes.append(f"search \"{q}\": no results")

            if not all_sources:
                return ResearchResult(
                    query=query,
                    ok=False,
                    notes=[*search_notes, "no results from any query"],
                )

            # 5. Deduplicate and rank
            deduped = deduplicate_sources(all_sources)
            ranked = rank_sources(deduped, strategy)

            # Filter out blocked sources (score < 0)
            ranked_ok = [(s, score) for s, score in ranked if score >= 0]
            blocked_count = len(ranked) - len(ranked_ok)

            # Pick top N to open
            max_open = strategy.max_pages_to_open or DEFAULT_LIMITS.max_pages
            top_sources = [s for s, _ in ranked_ok[:max_open]]

            ranking_notes: list[str] = []
            if blocked_count > 0:
                ranking_notes.append(f"{blocked_count} source(s) blocked by domain/avoid rules")
            ranking_notes.append(f"opening top {len(top_sources)}/{len(ranked_ok)} ranked sources")

            # 6. Open pages
            deadline = Deadline(limits.timeout_seconds)
            collected: list[Evidence] = []
            open_notes: list[str] = []

            open_steps = plan_opens(top_sources, max_open)
            for open_step in open_steps:
                if deadline.expired():
                    open_notes.append("deadline reached, stopped opening pages")
                    break

                page = researcher.open(open_step.payload)
                clean_text = page.clean_text.strip()
                if clean_text.startswith("Error:"):
                    open_notes.append(f"{open_step.payload}: {clean_text}")
                    continue

                if clean_text:
                    matching_source: Source | None = None
                    for s in top_sources:
                        if s.url == open_step.payload:
                            matching_source = s
                            break
                    if matching_source is None:
                        matching_source = Source(url=open_step.payload, title=page.title)
                    elif page.url and page.url != matching_source.url:
                        matching_source = Source(url=page.url, title=page.title or matching_source.title)

                    evidence = Evidence(
                        source=matching_source,
                        text=clean_text[: (strategy.max_evidence_chars or _DEFAULT_MAX_EVIDENCE_CHARS)],
                        fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    collected.append(evidence)

            # 7. Build notes with strategy metadata
            notes: list[str] = []
            if strategy.freshness:
                notes.append(f"freshness: {strategy.freshness}")
            if strategy.source_goal:
                notes.append(f"source_goal: {strategy.source_goal}")
            if strategy.answer_shape:
                notes.append(f"answer_shape: {strategy.answer_shape}")
            notes.extend(search_notes)
            notes.extend(ranking_notes)
            notes.extend(open_notes)

            max_ec = strategy.max_evidence_chars or _DEFAULT_MAX_EVIDENCE_CHARS
            total_evidence_chars = sum(len(e.text) for e in collected)
            if total_evidence_chars >= max_ec:
                notes.append(f"evidence truncated to {max_ec} chars per page (total {total_evidence_chars})")

            if collected:
                return ResearchResult(
                    query=query,
                    sources=top_sources,
                    evidence=collected,
                    ok=True,
                    notes=notes,
                )
            else:
                return ResearchResult(
                    query=query,
                    sources=top_sources,
                    evidence=collected,
                    ok=False,
                    notes=[*notes, "all pages returned empty content"],
                )
        finally:
            researcher.close()
    except Exception as exc:
        return ResearchResult(query=query, ok=False, notes=[f"research failed: {exc}"])
