"""Orchestration for the bundled Web Research Drone."""

from __future__ import annotations

import datetime as dt
import json
import sys

from browser_search import BrowserResearchSession
from evidence import (
    _answerable_sources,
    _source_conflict_gaps,
    _stale_source_gaps,
    select_relevant_evidence,
)
from fetching import discover_candidate_sources, discover_sources_with_gaps, fetch_sources
from models import ExtractedAnswer, FetchedSource
from query import _parse_query_from_goal, parse_research_query
from receipt import build_failure_receipt, build_result
from schedule import extract_schedule_answer
from synthesis import _configured_model_synthesis, _local_synthesis
from validate import _enforce_confidence_rules


def extract_answer(
    query: str,
    tags: list[str],
    fetched_sources: list[FetchedSource],
    now: dt.datetime,
) -> ExtractedAnswer:
    ok_sources = [source for source in fetched_sources if source.ok and source.text.strip()]
    if not ok_sources:
        return ExtractedAnswer(
            answer="",
            verified_facts=[],
            evidence=[],
            gaps=["No useful evidence was fetched."],
            confidence="none",
        )

    evidence_sources = _answerable_sources(ok_sources)
    selected_chunks = select_relevant_evidence(query, evidence_sources, tags)

    if "schedule" in tags:
        extracted = extract_schedule_answer(query, evidence_sources, now)
        assert isinstance(extracted, ExtractedAnswer)
        if extracted.answer:
            extracted.gaps.extend(_stale_source_gaps(query, tags, selected_chunks, now))
            extracted.gaps.extend(_source_conflict_gaps(query, tags, selected_chunks))
            return _enforce_confidence_rules(extracted, ok_sources)

    model_extracted = _configured_model_synthesis(query, selected_chunks, now)
    if model_extracted is not None:
        model_extracted.gaps.extend(_stale_source_gaps(query, tags, selected_chunks, now))
        model_extracted.gaps.extend(_source_conflict_gaps(query, tags, selected_chunks))
        return _enforce_confidence_rules(model_extracted, ok_sources)

    local = _local_synthesis(query, tags, selected_chunks, now)
    if "schedule" in tags and not local.answer:
        schedule_attempt = extract_schedule_answer(query, evidence_sources, now)
        assert isinstance(schedule_attempt, ExtractedAnswer)
        local.gaps = schedule_attempt.gaps + [gap for gap in local.gaps if gap not in schedule_attempt.gaps]
        if not local.evidence and schedule_attempt.evidence:
            local.evidence = schedule_attempt.evidence
    return _enforce_confidence_rules(local, ok_sources)


def run_query(query: str, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now().astimezone()
    parsed = parse_research_query(query)
    tags = parsed.tags
    with BrowserResearchSession() as browser_session:
        discovery = discover_sources_with_gaps(query, tags, browser_session=browser_session)
        targets = discovery.targets
        initial_sources = fetch_sources(targets, now, browser_session=browser_session)
        candidate_targets = discover_candidate_sources(initial_sources, targets)
        candidate_sources = (
            fetch_sources(candidate_targets, now, browser_session=browser_session)
            if candidate_targets
            else []
        )
        targets = targets + candidate_targets
        fetched_sources = initial_sources + candidate_sources
        discovery_metadata = discovery.route_metadata or browser_session.route_metadata
    extracted = extract_answer(query, tags, fetched_sources, now)
    for gap in discovery.gaps:
        if gap not in extracted.gaps:
            extracted.gaps.append(gap)
    return build_result(
        query,
        tags,
        targets,
        fetched_sources,
        extracted,
        discovery_metadata=discovery_metadata,
    )


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        payload: dict = {}
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            result = build_failure_receipt(
                f"Invalid JSON input: {exc}",
                "Web Research Drone could not run because the input was not valid JSON.",
            )
            print(json.dumps(result))
            return

    goal = payload.get("goal", "")
    query = _parse_query_from_goal(goal)
    if query is None:
        result = build_failure_receipt(
            "query is required",
            "Web Research Drone could not run because no query was provided.",
        )
        print(json.dumps(result))
        return

    result = run_query(query)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        result = build_failure_receipt(
            str(exc),
            f"Web Research Drone encountered an error: {exc}",
        )
        print(json.dumps(result))
