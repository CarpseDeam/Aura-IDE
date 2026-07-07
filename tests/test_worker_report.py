"""Focused tests for aura/conversation/worker_completion/_summary_formatters.py
and aura/bridge/worker_report.py.

Covers:
- Contradictory validation payloads rendering as product failures.
- Summary pass count using validation truth helpers, not raw ok.
- _final_report_claims_failure and _final_report_claims_validation helpers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from aura.bridge.worker_report import _build_worker_summary
from aura.conversation.worker_completion._summary_formatters import (
    _final_report_claims_failure,
    _final_report_claims_validation,
    _parse_structured_worker_failure,
    _format_structured_worker_failure,
    _format_worker_write_failure,
    _format_recoverable_write_failure,
)


# =========================================================================
# Contradictory validation payloads
# =========================================================================


class TestContradictoryPayloads:
    """Payloads where ok=True but exit_code=1 / counts_as_product_failure=True
    must still render as product failures, not passes."""

    def _summary_with_validation(self, *validation_results: dict) -> str:
        """Build a summary with the given validation results."""
        req = MagicMock()
        req.summary = ""
        req.goal = "test"
        return _build_worker_summary(
            req=req,
            history=MagicMock(),
            writes=[],
            errors=[],
            validation_results=list(validation_results),
        )

    def test_contradictory_ok_true_exit_1_product_failure_renders_as_failure(
        self,
    ) -> None:
        """A validation result with ok=True, exit_code=1, and
        counts_as_product_failure=True must appear under 'Product failures',
        not under 'Validated'."""
        summary = self._summary_with_validation({
            "command": "pytest",
            "ok": True,
            "exit_code": 1,
            "counts_as_product_failure": True,
            "output": "FAILED test_foo.py",
            "validation_classification": "product_validation_failed",
        })
        assert "Product failures" in summary, (
            "contradictory payload must render as product failure, not pass"
        )
        assert "✓" not in summary.split("pass")[0] if "✓" in summary else True
        assert "pytest" in summary

    def test_contradictory_not_in_validated_section(self) -> None:
        """A contradictory payload must NOT appear under 'Validated'."""
        summary = self._summary_with_validation({
            "command": "pytest",
            "ok": True,
            "exit_code": 1,
            "counts_as_product_failure": True,
            "output": "FAILED test_foo.py",
            "validation_classification": "product_validation_failed",
        })
        # The Validated section only appears if some payloads pass
        validated_section = self._section_after(summary, "Validated:")
        if validated_section:
            assert "pytest" not in validated_section.split("\n")[0]

    def test_contradictory_has_exit_code_in_display(self) -> None:
        """The failing display should include the exit code."""
        summary = self._summary_with_validation({
            "command": "pytest",
            "ok": True,
            "exit_code": 1,
            "counts_as_product_failure": True,
            "output": "FAILED test_foo.py",
            "validation_classification": "product_validation_failed",
        })
        assert "exit 1" in summary

    def test_honest_pass_not_affected(self) -> None:
        """A normal validation pass (exit_code=0, passed) must still appear
        under 'Validated'."""
        summary = self._summary_with_validation({
            "command": "ruff",
            "ok": True,
            "exit_code": 0,
            "counts_as_product_failure": False,
            "output": "All checks passed",
            "validation_classification": "passed",
        })
        assert "Validated:" in summary
        assert "ruff" in summary.split("Validated:")[1]

    @staticmethod
    def _section_after(text: str, header: str) -> str:
        """Return text after *header*, or empty string if not found."""
        if header not in text:
            return ""
        return text.split(header, 1)[1]


# =========================================================================
# Pass count uses validation truth helpers
# =========================================================================


class TestPassCountUsesValidationTruth:
    """The summary pass count must use validation_payload_passed(), not raw ok."""

    def _summary_validation_line(self, *validation_results: dict) -> str:
        """Extract the Validation glance line from a built summary."""
        req = MagicMock()
        req.summary = ""
        req.goal = "test"
        summary = _build_worker_summary(
            req=req,
            history=MagicMock(),
            writes=[],
            errors=[],
            validation_results=list(validation_results),
        )
        for line in summary.split("\n"):
            if "Validation" in line and ("/" in line or "not yet verified" in line):
                return line.strip()
        return ""

    def test_ok_true_exit_0_passed_counts_as_pass(self) -> None:
        """A validation with ok=True, exit_code=0, passed counts toward
        the pass count."""
        line = self._summary_validation_line({
            "command": "pytest",
            "ok": True,
            "exit_code": 0,
            "validation_classification": "passed",
            "counts_as_product_failure": False,
        })
        assert "1/1 passed" in line

    def test_ok_true_exit_1_does_not_count_as_pass(self) -> None:
        """A payload with ok=True but exit_code=1 must NOT count as passed."""
        line = self._summary_validation_line({
            "command": "pytest",
            "ok": True,
            "exit_code": 1,
            "counts_as_product_failure": True,
            "validation_classification": "product_validation_failed",
        })
        assert "0/1 passed" in line

    def test_ok_false_passed_counts_as_one_pass_one_fail(self) -> None:
        """Mixed results: one pass and one product failure."""
        line = self._summary_validation_line(
            {
                "command": "ruff",
                "ok": True,
                "exit_code": 0,
                "validation_classification": "passed",
                "counts_as_product_failure": False,
            },
            {
                "command": "pytest",
                "ok": False,
                "exit_code": 1,
                "counts_as_product_failure": True,
                "validation_classification": "product_validation_failed",
            },
        )
        assert "1/2 passed" in line

    def test_ok_true_exit_0_counts_as_product_failure_true_not_passed(
        self,
    ) -> None:
        """Even with exit_code=0, if counts_as_product_failure is True, the
        result is not considered passed."""
        line = self._summary_validation_line({
            "command": "pytest",
            "ok": True,
            "exit_code": 0,
            "counts_as_product_failure": True,
            "validation_classification": "product_validation_failed",
        })
        assert "0/1 passed" in line

    def test_empty_validation_results(self) -> None:
        """No validation results shows the 'not yet verified' message."""
        line = self._summary_validation_line()
        assert "not yet verified" in line

    def test_py_compile_special_case(self) -> None:
        """py_compile results use the compact py_compile display format."""
        line = self._summary_validation_line(
            {
                "command": "python -m py_compile a.py",
                "ok": True,
                "exit_code": 0,
                "validation_classification": "passed",
                "counts_as_product_failure": False,
            },
            {
                "command": "python -m py_compile b.py",
                "ok": True,
                "exit_code": 0,
                "validation_classification": "passed",
                "counts_as_product_failure": False,
            },
        )
        # py_compile block shows its own summary
        assert "py_compile" in line


# =========================================================================
# _final_report_claims_failure
# =========================================================================


class TestFinalReportClaimsFailure:
    """_final_report_claims_failure detects failure language in content."""

    def test_blocker_mentioned(self) -> None:
        assert _final_report_claims_failure("Found a blocker in the build") is True

    def test_validation_failed(self) -> None:
        assert _final_report_claims_failure("validation failed for pytest") is True

    def test_failed_validation(self) -> None:
        assert _final_report_claims_failure("failed validation for pytest") is True

    def test_could_not_verify(self) -> None:
        assert _final_report_claims_failure("could not verify the output") is True

    def test_could_not_run(self) -> None:
        assert _final_report_claims_failure("could not run pytest") is True

    def test_tests_failed(self) -> None:
        assert _final_report_claims_failure("2 tests failed") is True

    def test_clean_pass_is_not_failure(self) -> None:
        assert _final_report_claims_failure("All tests passed") is False

    def test_no_blocker_negation(self) -> None:
        """'no blockers' must not claim failure."""
        assert _final_report_claims_failure("no blockers found") is False

    def test_blocker_after_no_blocker(self) -> None:
        """'no blockers found but ... actual blocker' — the 'no blockers'
        negation is stripped, then 'blocker' still matches."""
        assert (
            _final_report_claims_failure(
                "no blockers found but there was a blocker"
            )
            is True
        )

    def test_empty_string(self) -> None:
        assert _final_report_claims_failure("") is False


# =========================================================================
# _final_report_claims_validation
# =========================================================================


class TestFinalReportClaimsValidation:
    """_final_report_claims_validation detects validation language."""

    def test_verified(self) -> None:
        assert _final_report_claims_validation("verified all outputs") is True

    def test_validated(self) -> None:
        assert _final_report_claims_validation("validated via pytest") is True

    def test_pytest_mentioned(self) -> None:
        assert _final_report_claims_validation("ran pytest") is True

    def test_tests_passed(self) -> None:
        assert _final_report_claims_validation("all tests passed") is True

    def test_exit_code_zero(self) -> None:
        assert _final_report_claims_validation("exit code 0") is True

    def test_no_validation(self) -> None:
        assert _final_report_claims_validation("no changes needed") is False

    def test_not_validated_negation(self) -> None:
        """'not validated' must not claim validation."""
        assert _final_report_claims_validation("not validated yet") is False

    def test_not_verified_negation(self) -> None:
        assert _final_report_claims_validation("not verified") is False

    def test_validation_after_not_negation(self) -> None:
        """'not validated yet but ... validated' strips 'not validated'
        then matches 'validated'."""
        assert (
            _final_report_claims_validation(
                "not validated yet, but validated after fix"
            )
            is True
        )

    def test_empty_string(self) -> None:
        assert _final_report_claims_validation("") is False


# =========================================================================
# _parse_structured_worker_failure
# =========================================================================


class TestParseStructuredWorkerFailure:
    """_parse_structured_worker_failure extracts structured failure metadata."""

    def test_ok_false_with_failure_class_returns_dict(self) -> None:
        result = _parse_structured_worker_failure(
            '{"ok": false, "failure_class": "syntax_invalid", "error": "Syntax error"}'
        )
        assert result == {"ok": False, "failure_class": "syntax_invalid", "error": "Syntax error"}

    def test_ok_true_returns_empty(self) -> None:
        assert _parse_structured_worker_failure('{"ok": true}') == {}

    def test_not_json_returns_empty(self) -> None:
        assert _parse_structured_worker_failure("not json") == {}

    def test_empty_string_returns_empty(self) -> None:
        assert _parse_structured_worker_failure("") == {}


# =========================================================================
# Formatting helpers
# =========================================================================


class TestFormatStructuredWorkerFailure:
    def test_basic_format(self) -> None:
        text = _format_structured_worker_failure(
            {"error": "Syntax error", "failure_class": "syntax_invalid"}
        )
        assert "Syntax error" in text
        assert "syntax_invalid" in text

    def test_with_detail(self) -> None:
        text = _format_structured_worker_failure(
            {
                "error": "Write failed",
                "failure_class": "write_failed",
                "details": {"path": "/tmp/x.py", "tool": "write_file"},
            }
        )
        assert "/tmp/x.py" in text
        assert "write_file" in text


class TestFormatWorkerWriteFailure:
    def test_basic(self) -> None:
        text = _format_worker_write_failure(
            {"name": "write_file", "path": "a.py", "error": "permission denied",
             "failure_class": "permission_error"}
        )
        assert "write_file" in text
        assert "a.py" in text
        assert "permission denied" in text


class TestFormatRecoverableWriteFailure:
    def test_basic(self) -> None:
        text = _format_recoverable_write_failure(
            {"name": "patch_file", "path": "b.py", "failure_class": "patch_failed",
             "suggested_next_tool": "write_file"}
        )
        assert "patch_file" in text
        assert "b.py" in text
        assert "write_file" in text
