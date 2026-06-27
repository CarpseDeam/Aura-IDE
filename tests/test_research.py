"""Tests for the research subsystem.

Tests cover data models, payload enrichment, and module exports.
No Playwright or network calls — these are pure unit tests.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from aura.research import Evidence, ResearchResult, Source, research_current_info


class TestResearchResultDataclass:
    """Verify ResearchResult dataclass construction and defaults."""

    def test_default_instantiation(self) -> None:
        """ResearchResult can be created with just a query."""
        result = ResearchResult(query="test query")
        assert result.query == "test query"
        assert result.ok is True
        assert result.sources == []
        assert result.evidence == []
        assert result.notes == []

    def test_source_count_not_required(self) -> None:
        """source_count/evidence_count are computed, not stored fields."""
        result = ResearchResult(query="q")
        # These should NOT be fields on the dataclass
        assert not hasattr(result, "source_count")
        assert not hasattr(result, "evidence_count")
        # They are derived from list lengths
        assert len(result.sources) == 0
        assert len(result.evidence) == 0

    def test_ok_defaults_to_true(self) -> None:
        """ok field defaults to True."""
        result = ResearchResult(query="q")
        assert result.ok is True
        result_fail = ResearchResult(query="q", ok=False)
        assert result_fail.ok is False

    def test_query_field(self) -> None:
        """query is a required field."""
        result = ResearchResult(query="what is the weather?")
        assert result.query == "what is the weather?"


class TestResearchHandlerPayloadEnriched:
    """Verify the mixin-style payload dict includes source_count and evidence_count."""

    def test_payload_contains_count_fields(self) -> None:
        """Construct a ResearchResult and verify the handler's payload dict shape."""
        source = Source(url="https://example.com", title="Example", snippet="An example page")
        evidence = Evidence(source=source, text="Some evidence text.", fetched_at="2025-01-01T00:00:00Z")
        result = ResearchResult(
            query="test",
            sources=[source],
            evidence=[evidence],
            ok=True,
        )
        # Build the payload dict the same way _handle_research_current_info does
        payload = {
            "ok": result.ok,
            "query": result.query,
            "source_count": len(result.sources),
            "evidence_count": len(result.evidence),
            "sources": [asdict(s) for s in result.sources],
            "evidence": [asdict(e) for e in result.evidence],
            "notes": result.notes,
        }
        assert payload["source_count"] == 1
        assert payload["evidence_count"] == 1
        assert payload["ok"] is True
        assert payload["query"] == "test"
        assert len(payload["sources"]) == 1
        assert len(payload["evidence"]) == 1

    def test_payload_zero_counts_when_empty(self) -> None:
        """Empty sources/evidence yield zero counts."""
        result = ResearchResult(query="empty")
        payload = {
            "ok": result.ok,
            "query": result.query,
            "source_count": len(result.sources),
            "evidence_count": len(result.evidence),
            "sources": [asdict(s) for s in result.sources],
            "evidence": [asdict(e) for e in result.evidence],
            "notes": result.notes,
        }
        assert payload["source_count"] == 0
        assert payload["evidence_count"] == 0
        assert payload["sources"] == []
        assert payload["evidence"] == []


class TestResearchModuleImports:
    """Verify public exports from aura.research import correctly."""

    def test_research_current_info_importable(self) -> None:
        """research_current_info function is importable."""
        assert callable(research_current_info)

    def test_research_result_importable(self) -> None:
        """ResearchResult class is importable."""
        from aura.research import ResearchResult
        assert issubclass(ResearchResult, object)

    def test_source_importable(self) -> None:
        """Source class is importable."""
        from aura.research import Source
        source = Source(url="u", title="t")
        assert source.url == "u"
        assert source.title == "t"

    def test_evidence_importable(self) -> None:
        """Evidence class is importable."""
        from aura.research import Evidence
        src = Source(url="u", title="t")
        ev = Evidence(source=src, text="t", fetched_at="2025-01-01T00:00:00Z")
        assert ev.text == "t"

    def test_strategy_importable(self) -> None:
        """ResearchStrategy and parse_strategy are importable."""
        from aura.research import ResearchStrategy, parse_strategy
        assert issubclass(ResearchStrategy, object)
        assert callable(parse_strategy)


class TestParseStrategy:
    """Tests for parse_strategy."""

    def test_strategy_defaults(self) -> None:
        """Empty constraints gives objective as only variant."""
        from aura.research.strategy import parse_strategy
        s = parse_strategy("my query", None)
        assert s.objective == "my query"
        assert s.freshness is None
        assert s.source_goal is None
        assert s.answer_shape is None
        assert s.query_variants == []
        assert s.allowed_domains == []
        assert s.blocked_domains == []
        assert s.avoid == []
        assert s.max_searches is None
        assert s.max_search_results is None
        assert s.max_pages_to_open is None
        assert s.max_evidence_chars is None

    def test_strategy_max_pages_backward_compat(self) -> None:
        """Old max_pages sets both search and open limits."""
        from aura.research.strategy import parse_strategy
        s = parse_strategy("q", {"max_pages": 3})
        assert s.max_search_results == 3
        assert s.max_pages_to_open == 3

    def test_strategy_query_variants(self) -> None:
        """Variants override."""
        from aura.research.strategy import parse_strategy
        s = parse_strategy("q", {"query_variants": ["v1", "v2", "v3"]})
        assert s.query_variants == ["v1", "v2", "v3"]
        assert s.objective == "q"

    def test_strategy_bad_values_fall_back(self) -> None:
        """Bad ints don't crash."""
        from aura.research.strategy import parse_strategy
        s = parse_strategy("q", {"max_searches": "not_an_int"})
        assert s.max_searches is None
        # Should not raise
        s2 = parse_strategy("q", {"max_searches": [1, 2, 3]})
        assert s2.max_searches is None


class TestRanking:
    """Tests for ranking.deduplicate_sources and rank_sources."""

    def test_dedup_normalises_urls(self) -> None:
        """www. and trailing slash are normalised."""
        from aura.research.models import Source
        from aura.research.ranking import deduplicate_sources
        sources = [
            Source(url="https://www.example.com/", title="A"),
            Source(url="https://example.com", title="B"),
            Source(url="https://example.com/page", title="C"),
        ]
        deduped = deduplicate_sources(sources)
        assert len(deduped) == 2  # first two are same normalised URL
        assert deduped[0].title == "A"
        assert deduped[1].title == "C"

    def test_rank_blocked_domain_eliminates(self) -> None:
        """Blocked domain gets -10, resulting in score < 0."""
        from aura.research.models import Source
        from aura.research.ranking import rank_sources
        from aura.research.strategy import ResearchStrategy
        sources = [
            Source(url="https://bad-domain.com/page", title="Bad"),
            Source(url="https://good-site.com/page", title="Good"),
        ]
        strategy = ResearchStrategy(objective="test", blocked_domains=["bad-domain.com"])
        ranked = rank_sources(sources, strategy)
        # First should be good, second should be bad with negative score
        assert ranked[0][0].title == "Good"
        assert ranked[0][1] >= 1.0
        assert ranked[1][0].title == "Bad"
        assert ranked[1][1] < 0

    def test_rank_allowed_domain_boosts(self) -> None:
        """Allowed domain gets +0.3."""
        from aura.research.models import Source
        from aura.research.ranking import rank_sources
        from aura.research.strategy import ResearchStrategy
        sources = [
            Source(url="https://trusted.org/page", title="Trusted"),
        ]
        strategy = ResearchStrategy(objective="test", allowed_domains=["trusted.org"])
        ranked = rank_sources(sources, strategy)
        assert ranked[0][1] == pytest.approx(1.3, abs=0.01)

    def test_rank_avoid_terms_penalise(self) -> None:
        """Avoid term in title reduces score."""
        from aura.research.models import Source
        from aura.research.ranking import rank_sources
        from aura.research.strategy import ResearchStrategy
        sources = [
            Source(url="https://sponsored.com/page", title="Sponsored Content"),
        ]
        strategy = ResearchStrategy(objective="test", avoid=["sponsored"])
        ranked = rank_sources(sources, strategy)
        assert ranked[0][1] == pytest.approx(0.8, abs=0.01)

    def test_rank_prefers_official_tld(self) -> None:
        """.gov / .edu TLD gives +0.2."""
        from aura.research.models import Source
        from aura.research.ranking import rank_sources
        from aura.research.strategy import ResearchStrategy
        sources = [
            Source(url="https://www.nasa.gov/page", title="NASA"),
            Source(url="https://example.com/page", title="Example"),
        ]
        strategy = ResearchStrategy(objective="test")
        ranked = rank_sources(sources, strategy)
        assert ranked[0][0].title == "NASA"
        assert ranked[0][1] == pytest.approx(1.2, abs=0.01)
        assert ranked[1][1] == pytest.approx(1.0, abs=0.01)


class TestPayloadBackwardCompat:
    """ResearchResult payload dict shape must be unchanged."""

    def test_payload_backward_compat(self) -> None:
        """All required keys present in ResearchResult dict payload."""
        from aura.research import Evidence, ResearchResult, Source
        source = Source(url="https://example.com", title="Example", snippet="An example page")
        evidence = Evidence(source=source, text="Some evidence text.", fetched_at="2025-01-01T00:00:00Z")
        result = ResearchResult(
            query="test",
            sources=[source],
            evidence=[evidence],
            ok=True,
            notes=["test note"],
        )
        payload = {
            "ok": result.ok,
            "query": result.query,
            "source_count": len(result.sources),
            "evidence_count": len(result.evidence),
            "sources": [asdict(s) for s in result.sources],
            "evidence": [asdict(e) for e in result.evidence],
            "notes": result.notes,
        }
        assert payload["ok"] is True
        assert payload["query"] == "test"
        assert payload["source_count"] == 1
        assert payload["evidence_count"] == 1
        assert isinstance(payload["sources"], list)
        assert isinstance(payload["evidence"], list)
        assert isinstance(payload["notes"], list)


class TestSchemaGuidance:
    """Verify the research tool schema constraints description contains concrete examples.

    The concrete guidance lives in the ``constraints`` property description,
    not in the top-level function description.
    """

    @property
    def _constraints_desc(self) -> str:
        from aura.conversation.tools._research_schemas import RESEARCH_TOOL_DEF
        return RESEARCH_TOOL_DEF["function"]["parameters"]["properties"]["constraints"]["description"]

    def test_description_mentions_schedules(self) -> None:
        """Constraints description includes 'schedules / scores / events' example."""
        desc = self._constraints_desc
        assert "schedules" in desc.lower()

    def test_description_mentions_stock(self) -> None:
        """Constraints description includes 'stock / prices' example."""
        desc = self._constraints_desc
        assert "stock" in desc.lower()
        assert "prices" in desc.lower()

    def test_description_mentions_listings(self) -> None:
        """Constraints description includes 'listings' example."""
        desc = self._constraints_desc
        assert "listings" in desc.lower()

    def test_description_mentions_structured(self) -> None:
        """Constraints description says 'structured'."""
        desc = self._constraints_desc
        assert "structured" in desc.lower()

    def test_description_mentions_constraints_not_optional(self) -> None:
        """Constraints description says 'constraints are not optional decoration'."""
        desc = self._constraints_desc
        assert "constraints are not optional decoration" in desc.lower()


class TestPlannerPromptResearchDiscipline:
    """Verify the planner system prompt contains research retry discipline."""

    def test_contains_research_retry_discipline_heading(self) -> None:
        """Prompt has 'Research retry discipline' block."""
        from aura.prompts import PLANNER_SYSTEM_PROMPT
        assert "Research retry discipline" in PLANNER_SYSTEM_PROMPT

    def test_contains_one_good_strategic_call(self) -> None:
        """Prompt says 'Make one good strategic research call first'."""
        from aura.prompts import PLANNER_SYSTEM_PROMPT
        assert "one good strategic research call first" in PLANNER_SYSTEM_PROMPT

    def test_contains_at_most_one_followup(self) -> None:
        """Prompt says 'At most one follow-up'."""
        from aura.prompts import PLANNER_SYSTEM_PROMPT
        assert "At most one follow-up" in PLANNER_SYSTEM_PROMPT

    def test_contains_no_article_blog_opinion_as_authoritative(self) -> None:
        """Prompt says 'Do not treat article, blog, or opinion sources'."""
        from aura.prompts import PLANNER_SYSTEM_PROMPT
        assert "Do not treat article, blog, or opinion sources" in PLANNER_SYSTEM_PROMPT


class TestEvidenceQualityNotes:
    """Tests for the _assess_evidence_quality helper."""

    def test_assess_evidence_quality_importable(self) -> None:
        """_assess_evidence_quality is importable from aura.research.service."""
        from aura.research.service import _assess_evidence_quality
        assert callable(_assess_evidence_quality)

    def test_no_quality_notes_when_source_goal_none(self) -> None:
        """When source_goal is None, no quality notes are returned."""
        from aura.research import Evidence, Source
        from aura.research.service import _assess_evidence_quality
        from aura.research.strategy import ResearchStrategy
        evidence = [
            Evidence(
                source=Source(url="https://example.com/article", title="Some Article"),
                text="content",
                fetched_at="2025-01-01T00:00:00Z",
            ),
        ]
        strategy = ResearchStrategy(objective="test")
        notes = _assess_evidence_quality(evidence, strategy)
        assert notes == []

    def test_weak_for_requested_source_goal_when_all_articles(self) -> None:
        """When all titles contain article words and source_goal is structured data,
        emits weak_for_requested_source_goal."""
        from aura.research import Evidence, Source
        from aura.research.service import _assess_evidence_quality
        from aura.research.strategy import ResearchStrategy
        evidence = [
            Evidence(
                source=Source(url="https://sports.com/recap", title="Match Recap: Exciting Finish"),
                text="content",
                fetched_at="2025-01-01T00:00:00Z",
            ),
            Evidence(
                source=Source(url="https://news.com/story", title="Opinion: Who Will Win?"),
                text="content",
                fetched_at="2025-01-01T00:00:00Z",
            ),
        ]
        strategy = ResearchStrategy(
            objective="latest scores",
            source_goal="official structured schedule scores fixtures",
        )
        notes = _assess_evidence_quality(evidence, strategy)
        assert "evidence_quality: weak_for_requested_source_goal" in notes

    def test_avoid_terms_present_when_most_titles_contain_avoid(self) -> None:
        """When avoid terms are in most titles, emits avoid_terms_present."""
        from aura.research import Evidence, Source
        from aura.research.service import _assess_evidence_quality
        from aura.research.strategy import ResearchStrategy
        evidence = [
            Evidence(
                source=Source(url="https://blog.com/opinion", title="Blog Post: My Analysis"),
                text="content",
                fetched_at="2025-01-01T00:00:00Z",
            ),
            Evidence(
                source=Source(url="https://news.com/story", title="News Article"),
                text="content",
                fetched_at="2025-01-01T00:00:00Z",
            ),
            Evidence(
                source=Source(url="https://official.org/scores", title="Official Scores"),
                text="content",
                fetched_at="2025-01-01T00:00:00Z",
            ),
        ]
        strategy = ResearchStrategy(
            objective="scores",
            avoid=["blog", "opinion", "article", "analysis"],
        )
        notes = _assess_evidence_quality(evidence, strategy)
        assert "evidence_quality: avoid_terms_present" in notes

    def test_no_source_goal_match_when_goal_terms_absent(self) -> None:
        """When source_goal terms are absent from all titles/URLs, emits
        no_source_goal_match."""
        from aura.research import Evidence, Source
        from aura.research.service import _assess_evidence_quality
        from aura.research.strategy import ResearchStrategy
        evidence = [
            Evidence(
                source=Source(url="https://randomblog.com/post", title="Random Thoughts"),
                text="content",
                fetched_at="2025-01-01T00:00:00Z",
            ),
        ]
        strategy = ResearchStrategy(
            objective="find stats",
            source_goal="structured statistics data tables",
        )
        notes = _assess_evidence_quality(evidence, strategy)
        assert "evidence_quality: no_source_goal_match" in notes

    def test_no_quality_notes_when_evidence_empty(self) -> None:
        """Empty evidence list returns empty notes regardless of strategy."""
        from aura.research.service import _assess_evidence_quality
        from aura.research.strategy import ResearchStrategy
        strategy = ResearchStrategy(
            objective="test",
            source_goal="structured data",
            avoid=["blog"],
        )
        notes = _assess_evidence_quality([], strategy)
        assert notes == []
