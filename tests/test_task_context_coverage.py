"""``read_task_context`` must report how much of the workspace it actually read.

Query and symbol lookups walk the workspace under a hard candidate-file cap. In
a large repository that means the scan is *partial*, which makes a "no hits"
answer ambiguous: the code may be absent, or it may live in a file the walk
never reached. Reporting that no-hit result without the caveat is how an agent
concludes something does not exist and then rewrites it from scratch.

What is asserted here:

* the candidate limit, files considered, partial flag, and stop reason are all
  reported;
* a partial scan that found nothing says explicitly that it is inconclusive;
* a complete scan does not cry wolf;
* the caveat reaches the model in the payload, not just the log.
"""

from __future__ import annotations

import json

import pytest

from aura.conversation.tools.task_context import (
    CANDIDATE_FILE_LIMIT,
    STOP_CANDIDATE_FILE_LIMIT,
    STOP_NOT_SCANNED,
    STOP_WORKSPACE_EXHAUSTED,
    read_task_context,
)


@pytest.fixture
def big_workspace(tmp_path):
    """More files than the candidate cap, none containing the search term."""
    for i in range(CANDIDATE_FILE_LIMIT + 100):
        (tmp_path / f"file_{i:04d}.py").write_text(
            f"value_{i} = {i}\n", encoding="utf-8"
        )
    return tmp_path


@pytest.fixture
def small_workspace(tmp_path):
    (tmp_path / "alpha.py").write_text("def hello_world():\n    pass\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("import alpha\n", encoding="utf-8")
    return tmp_path


# ── coverage is always reported ─────────────────────────────────────────────


class TestCoverageIsReported:

    def test_partial_coverage_reports_limit_considered_and_stop_reason(
        self, big_workspace
    ) -> None:
        result = read_task_context(big_workspace, {"query": "absent_term"})
        coverage = result["coverage"]

        assert coverage["candidate_file_limit"] == CANDIDATE_FILE_LIMIT
        assert coverage["files_considered"] == CANDIDATE_FILE_LIMIT
        assert coverage["partial"] is True
        assert coverage["stop_reason"] == STOP_CANDIDATE_FILE_LIMIT

    def test_a_500_file_cap_is_what_makes_it_partial(self, big_workspace) -> None:
        result = read_task_context(big_workspace, {"query": "absent_term"})

        assert result["coverage"]["files_considered"] == 500
        assert result["coverage"]["candidate_file_limit"] == 500

    def test_complete_coverage_is_reported_as_complete(self, small_workspace) -> None:
        result = read_task_context(small_workspace, {"query": "hello_world"})
        coverage = result["coverage"]

        assert coverage["partial"] is False
        assert coverage["stop_reason"] == STOP_WORKSPACE_EXHAUSTED
        assert coverage["files_considered"] < CANDIDATE_FILE_LIMIT

    def test_a_files_only_request_reports_that_nothing_was_scanned(
        self, small_workspace
    ) -> None:
        result = read_task_context(small_workspace, {"files": ["alpha.py"]})
        coverage = result["coverage"]

        assert coverage["scanned"] is False
        assert coverage["partial"] is False
        assert coverage["stop_reason"] == STOP_NOT_SCANNED

    def test_coverage_names_which_passes_ran(self, small_workspace) -> None:
        result = read_task_context(
            small_workspace, {"query": "hello", "symbols": ["alpha"]}
        )

        assert result["coverage"]["passes"] == ["query", "symbol:alpha"]

    def test_file_counts_are_not_double_counted_across_passes(
        self, small_workspace
    ) -> None:
        """Two passes over the same tree examined those files once, not twice."""
        one = read_task_context(small_workspace, {"query": "hello"})
        two = read_task_context(
            small_workspace, {"query": "hello", "symbols": ["alpha", "beta"]}
        )

        assert two["coverage"]["files_considered"] == one["coverage"]["files_considered"]

    def test_the_payload_is_serializable(self, big_workspace) -> None:
        result = read_task_context(big_workspace, {"query": "absent_term"})

        assert json.loads(json.dumps(result))["coverage"]["partial"] is True


# ── partial no-hit results are explicitly inconclusive ──────────────────────


class TestPartialNoHitIsInconclusive:

    def test_a_partial_query_miss_says_it_is_inconclusive(self, big_workspace) -> None:
        result = read_task_context(big_workspace, {"query": "absent_term"})
        caveats = " ".join(result["caveats"])

        assert "INCONCLUSIVE" in caveats
        assert "does NOT mean the code is absent" in caveats
        assert "500" in caveats

    def test_a_partial_symbol_miss_says_it_is_inconclusive_and_names_it(
        self, big_workspace
    ) -> None:
        result = read_task_context(big_workspace, {"symbols": ["MissingSymbol"]})
        caveats = " ".join(result["caveats"])

        assert "INCONCLUSIVE" in caveats
        assert "MissingSymbol" in caveats

    def test_the_inconclusive_note_also_appears_in_the_rendered_context(
        self, big_workspace
    ) -> None:
        """The model reads `context`; the warning has to be in there too."""
        result = read_task_context(big_workspace, {"symbols": ["MissingSymbol"]})

        assert "not evidence of absence" in result["context"]

    def test_partial_coverage_is_stated_even_when_hits_were_found(
        self, big_workspace
    ) -> None:
        (big_workspace / "file_0001.py").write_text(
            "findable_marker = 1\n", encoding="utf-8"
        )
        result = read_task_context(big_workspace, {"query": "findable_marker"})

        assert result["coverage"]["partial"] is True
        assert any("Partial workspace coverage" in c for c in result["caveats"])

    def test_a_complete_scan_miss_is_not_labelled_inconclusive(
        self, small_workspace
    ) -> None:
        result = read_task_context(small_workspace, {"query": "definitely_absent"})
        caveats = " ".join(result["caveats"])

        assert "INCONCLUSIVE" not in caveats
        assert "full workspace scan" in caveats

    def test_a_complete_symbol_miss_is_not_labelled_inconclusive(
        self, small_workspace
    ) -> None:
        result = read_task_context(small_workspace, {"symbols": ["NoSuchThing"]})
        caveats = " ".join(result["caveats"])

        assert "INCONCLUSIVE" not in caveats
        assert "NoSuchThing" in caveats

    def test_a_successful_lookup_carries_no_absence_caveat(
        self, small_workspace
    ) -> None:
        result = read_task_context(small_workspace, {"query": "hello_world"})
        caveats = " ".join(result["caveats"])

        assert "INCONCLUSIVE" not in caveats
        assert "no hits" not in caveats


# ── the limit stays configurable ────────────────────────────────────────────


class TestCandidateLimitIsConfigurable:

    def test_the_limit_is_a_named_constant(self) -> None:
        assert CANDIDATE_FILE_LIMIT == 500

    def test_a_caller_can_lower_the_limit(self, small_workspace) -> None:
        result = read_task_context(
            small_workspace, {"query": "absent", "max_candidate_files": 1}
        )
        coverage = result["coverage"]

        assert coverage["candidate_file_limit"] == 1
        assert coverage["partial"] is True
        assert "INCONCLUSIVE" in " ".join(result["caveats"])

    def test_a_nonsense_limit_falls_back_to_the_default(self, small_workspace) -> None:
        result = read_task_context(
            small_workspace, {"query": "hello", "max_candidate_files": "banana"}
        )

        assert result["coverage"]["candidate_file_limit"] == CANDIDATE_FILE_LIMIT


# ── existing behaviour is unchanged ─────────────────────────────────────────


class TestExistingBehaviourIntact:

    def test_the_result_still_carries_its_original_fields(self, small_workspace) -> None:
        result = read_task_context(small_workspace, {"files": ["alpha.py"]})

        for key in ("ok", "files", "query", "symbols", "context", "truncated", "caveats"):
            assert key in result
        assert result["ok"] is True
        assert result["files"] == ["alpha.py"]

    def test_missing_files_are_still_reported(self, small_workspace) -> None:
        result = read_task_context(small_workspace, {"files": ["nope.py"]})

        assert any("missing" in c for c in result["caveats"])

    def test_max_chars_truncation_still_works(self, small_workspace) -> None:
        result = read_task_context(
            small_workspace, {"files": ["alpha.py"], "max_chars": 20}
        )

        assert result["truncated"] is True
        assert any("max_chars" in c for c in result["caveats"])
