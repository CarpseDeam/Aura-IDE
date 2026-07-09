"""Tests for aura.conversation.syntax_probe_state."""

from __future__ import annotations

from aura.syntax_probe.models import SyntaxProbeResult
from aura.conversation.syntax_probe_state import apply_syntax_probe_result_to_state


class TestApplySyntaxProbeResultToState:
    """apply_syntax_probe_result_to_state behaviour for each evidence value."""

    def test_pass_clears_repair_state(self) -> None:
        syntax_repair_required = {
            "path/to/file.py": {"error": "old error", "failed_repairs": 1}
        }
        syntax_validation_required = {"path/to/file.py"}

        result = SyntaxProbeResult(
            path="path/to/file.py",
            language_id="python",
            evidence="pass",
        )
        apply_syntax_probe_result_to_state(
            result, syntax_repair_required, syntax_validation_required
        )

        assert syntax_repair_required == {}
        assert syntax_validation_required == set()

    def test_pass_clears_repair_state_and_appends_note(self) -> None:
        syntax_repair_required = {
            "path/to/file.py": {"error": "old error", "failed_repairs": 1}
        }
        syntax_validation_required = {"path/to/file.py"}
        stale_validation_notes: list[str] = []

        result = SyntaxProbeResult(
            path="path/to/file.py",
            language_id="python",
            evidence="pass",
        )
        apply_syntax_probe_result_to_state(
            result,
            syntax_repair_required,
            syntax_validation_required,
            stale_validation_notes=stale_validation_notes,
        )

        assert syntax_repair_required == {}
        assert syntax_validation_required == set()
        assert len(stale_validation_notes) == 1
        assert "syntax probe passed" in stale_validation_notes[0]

    def test_pass_no_prior_state_no_note(self) -> None:
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()
        stale_validation_notes: list[str] = []

        result = SyntaxProbeResult(
            path="path/to/file.py",
            language_id="python",
            evidence="pass",
        )
        apply_syntax_probe_result_to_state(
            result,
            syntax_repair_required,
            syntax_validation_required,
            stale_validation_notes=stale_validation_notes,
        )

        assert stale_validation_notes == []

    def test_fail_sets_repair_state(self) -> None:
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()

        result = SyntaxProbeResult(
            path="broken.py",
            language_id="python",
            evidence="fail",
            error="SyntaxError: ...",
            line=5,
            column=3,
            failure_class="syntax_invalid",
        )
        apply_syntax_probe_result_to_state(
            result, syntax_repair_required, syntax_validation_required
        )

        state = syntax_repair_required.get("broken.py")
        assert state is not None
        assert state["error"] == "SyntaxError: ..."
        assert state["line"] == 5
        assert state["column"] == 3
        assert state["language_id"] == "python"
        assert state["failure_class"] == "syntax_invalid"
        assert state["probe_evidence"] == "fail"
        assert state["awaiting_validation"] is False
        assert state["repair_failed"] is False
        assert state["failed_repairs"] == 0

    def test_fail_after_repair_attempted_marks_repair_failed(self) -> None:
        syntax_repair_required = {
            "broken.py": {
                "repair_attempted": True,
                "awaiting_validation": True,
                "failed_repairs": 1,
            }
        }
        syntax_validation_required: set[str] = set()

        result = SyntaxProbeResult(
            path="broken.py",
            language_id="python",
            evidence="fail",
            error="SyntaxError: ...",
            line=5,
            column=3,
            failure_class="syntax_invalid",
        )
        apply_syntax_probe_result_to_state(
            result, syntax_repair_required, syntax_validation_required
        )

        state = syntax_repair_required.get("broken.py")
        assert state is not None
        assert state["repair_failed"] is True
        assert state["failed_repairs"] == 2

    def test_fail_discards_validation_required(self) -> None:
        syntax_repair_required: dict = {}
        syntax_validation_required = {"broken.py"}

        result = SyntaxProbeResult(
            path="broken.py",
            language_id="python",
            evidence="fail",
            error="SyntaxError: ...",
            line=5,
            column=3,
        )
        apply_syntax_probe_result_to_state(
            result, syntax_repair_required, syntax_validation_required
        )

        assert "broken.py" not in syntax_validation_required

    def test_no_evidence_does_not_set_repair_state(self) -> None:
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()

        result = SyntaxProbeResult(
            path="unknown.py",
            language_id="python",
            evidence="no_evidence",
        )
        apply_syntax_probe_result_to_state(
            result, syntax_repair_required, syntax_validation_required
        )

        assert syntax_repair_required == {}
        assert syntax_validation_required == set()

    def test_no_evidence_does_not_clear_existing_state(self) -> None:
        syntax_repair_required = {"existing.py": {"error": "old"}}
        syntax_validation_required: set[str] = set()

        result = SyntaxProbeResult(
            path="existing.py",
            language_id="python",
            evidence="no_evidence",
        )
        apply_syntax_probe_result_to_state(
            result, syntax_repair_required, syntax_validation_required
        )

        assert syntax_repair_required == {"existing.py": {"error": "old"}}
