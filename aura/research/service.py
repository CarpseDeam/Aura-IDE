"""Public research service — entry point for web research."""

from __future__ import annotations

import datetime
from typing import Any

from aura.research.limits import DEFAULT_LIMITS, Deadline, ResearchLimits
from aura.research.models import Evidence, ResearchResult, Source
from aura.research.planner import plan_opens, plan_search
from aura.research.playwright import PlaywrightResearcher


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
        limits = DEFAULT_LIMITS
        if constraints:
            max_pages = constraints.get("max_pages")
            timeout_seconds = constraints.get("timeout_seconds")
            if max_pages is not None or timeout_seconds is not None:
                limits = ResearchLimits(
                    max_pages=int(max_pages) if max_pages is not None else DEFAULT_LIMITS.max_pages,
                    timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else DEFAULT_LIMITS.timeout_seconds,
                )

        researcher = PlaywrightResearcher(limits=limits)
        try:
            if not researcher.start():
                return ResearchResult(
                    query=query,
                    ok=False,
                    notes=[researcher._unavailable_reason or "browser backend unavailable"],
                )

            # Run the search
            plan_search(query)  # deterministic planning, result unused
            sources = researcher.search(query)

            if not sources:
                return ResearchResult(query=query, ok=False, notes=["no results"])

            deadline = Deadline(limits.timeout_seconds)
            collected: list[Evidence] = []

            open_steps = plan_opens(sources, limits)
            for open_step in open_steps:
                if deadline.expired():
                    break

                page = researcher.open(open_step.payload)
                clean_text = page.clean_text.strip()

                if clean_text:
                    # Find the matching Source by URL
                    matching_source: Source | None = None
                    for s in sources:
                        if s.url == open_step.payload:
                            matching_source = s
                            break
                    if matching_source is None:
                        matching_source = Source(url=open_step.payload, title=page.title)

                    evidence = Evidence(
                        source=matching_source,
                        text=clean_text[:8000],
                        fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )
                    collected.append(evidence)

            if collected:
                return ResearchResult(
                    query=query,
                    sources=sources,
                    evidence=collected,
                    ok=True,
                    notes=[f"researched {len(collected)} page(s)"],
                )
            else:
                return ResearchResult(
                    query=query,
                    sources=sources,
                    evidence=collected,
                    ok=False,
                    notes=["all pages returned empty content"],
                )
        finally:
            researcher.close()
    except Exception as exc:
        return ResearchResult(query=query, ok=False, notes=[f"research failed: {exc}"])
