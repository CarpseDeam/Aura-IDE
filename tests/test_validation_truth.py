"""Tests for aura/conversation/validation_truth.py — Phase 1 truth helpers."""
from __future__ import annotations

from aura.conversation.validation_truth import (
    normalize_validation_command_key,
    validation_payload_counts_as_validation,
    validation_payload_failed,
    validation_payload_passed,
    validation_payload_product_failure,
)


class TestValidationPayloadCountsAsValidation:
    """``counts_as_validation``, ``auto_validation``, or a classification."""

    def test_counts_as_validation_true(self):
        assert validation_payload_counts_as_validation({"counts_as_validation": True})

    def test_auto_validation_true(self):
        assert validation_payload_counts_as_validation({"auto_validation": True})

    def test_validation_classification_present(self):
        assert validation_payload_counts_as_validation(
            {"validation_classification": "passed"}
        )

    def test_validation_classification_present_counts(self):
        assert validation_payload_counts_as_validation(
            {"validation_classification": "product_validation_failed"}
        )

    def test_bare_classification_not_enough(self):
        """``classification`` alone (without ``validation_classification``)
        is not enough to count as validation — it is a generic field."""
        assert not validation_payload_counts_as_validation(
            {"classification": "product_validation_failed"}
        )

    def test_not_validation_when_none_set(self):
        assert not validation_payload_counts_as_validation(
            {"ok": True, "exit_code": 0, "command": "pytest"}
        )

    def test_false_values(self):
        assert not validation_payload_counts_as_validation(
            {"counts_as_validation": False}
        )


class TestValidationPayloadPassed:
    """Only ``validation_classification == "passed"`` or
    ``classification == "passed"`` with validation context means passed."""

    def test_validation_classification_passed(self):
        assert validation_payload_passed(
            {"validation_classification": "passed"}
        )

    def test_classification_passed_with_counts_as_validation(self):
        assert validation_payload_passed(
            {"classification": "passed", "counts_as_validation": True}
        )

    def test_classification_passed_with_auto_validation(self):
        assert validation_payload_passed(
            {"classification": "passed", "auto_validation": True}
        )

    def test_classification_passed_alone_not_enough(self):
        """``classification == "passed"`` without counts_as_validation or
        auto_validation is NOT treated as passed."""
        assert not validation_payload_passed(
            {"classification": "passed"}
        )

    def test_ok_true_alone_never_passed(self):
        """Raw terminal ok=True is never validation success."""
        assert not validation_payload_passed(
            {"ok": True, "exit_code": 0, "command": "pytest"}
        )

    def test_ok_true_with_product_failure_not_passed(self):
        """A validation that set both ok=True (terminal success) and
        product-validation-classification is NOT passed."""
        assert not validation_payload_passed(
            {
                "ok": True,
                "exit_code": 1,
                "validation_classification": "product_validation_failed",
                "counts_as_product_failure": True,
            }
        )

    def test_empty_payload_not_passed(self):
        assert not validation_payload_passed({})

    def test_passed_overrides_count(self):
        """validation_classification=passed wins even if
        counts_as_product_failure is also set (shouldn't happen, but
        the passthrough classification is authoritative)."""
        assert validation_payload_passed(
            {
                "validation_classification": "passed",
                "counts_as_product_failure": True,
            }
        )

    def test_command_outcome_classification_passed_with_validation(self):
        """``command_outcome_classification == "passed"`` with
        ``counts_as_validation`` is treated as passed — this is the
        primary field set by the tool runner for validation commands."""
        assert validation_payload_passed(
            {
                "command_outcome_classification": "passed",
                "counts_as_validation": True,
                "exit_code": 0,
            }
        )

    def test_command_outcome_classification_passed_without_validation(self):
        """``command_outcome_classification == "passed"`` without
        ``counts_as_validation`` is NOT passed — it's a regular
        command, not a validation attempt."""
        assert not validation_payload_passed(
            {
                "command_outcome_classification": "passed",
                "exit_code": 0,
            }
        )

    def test_validation_classification_passed_wins_over_command_outcome(self):
        """validation_classification takes precedence."""
        assert validation_payload_passed(
            {
                "validation_classification": "passed",
                "command_outcome_classification": "product_validation_failed",
                "exit_code": 1,
                "counts_as_product_failure": True,
            }
        )


class TestValidationPayloadFailed:
    """Failure is detected from exit_code, counts_as_product_failure,
    or command_success."""

    def test_counts_as_product_failure(self):
        assert validation_payload_failed(
            {"counts_as_product_failure": True}
        )

    def test_exit_code_non_zero(self):
        assert validation_payload_failed({"exit_code": 1})

    def test_exit_code_negative(self):
        assert validation_payload_failed({"exit_code": -1})

    def test_command_success_false(self):
        assert validation_payload_failed(
            {"command_success": False}
        )

    def test_exit_code_zero_not_failure(self):
        assert not validation_payload_failed({"exit_code": 0})

    def test_empty_payload_not_failed(self):
        assert not validation_payload_failed({})

    def test_exit_code_none_not_failed(self):
        assert not validation_payload_failed({"exit_code": None})

    def test_ok_true_exit_code_zero_not_failed(self):
        assert not validation_payload_failed(
            {"ok": True, "exit_code": 0}
        )

    def test_both_passed_classification_and_failure_fields(self):
        """failure detection returns True when fields indicate failure,
        even if a classification field also says passed.  The callers
        check ``passed`` first, so in practice this doesn't conflict."""
        result = validation_payload_failed(
            {
                "validation_classification": "passed",
                "exit_code": 1,
                "counts_as_product_failure": True,
            }
        )
        assert result is True


class TestValidationPayloadProductFailure:
    """Only counts_as_product_failure=True means product failure."""

    def test_true(self):
        assert validation_payload_product_failure(
            {"counts_as_product_failure": True}
        )

    def test_false(self):
        assert not validation_payload_product_failure(
            {"counts_as_product_failure": False}
        )

    def test_missing(self):
        assert not validation_payload_product_failure({})

    def test_exit_code_non_zero_not_product_failure(self):
        assert not validation_payload_product_failure({"exit_code": 1})


class TestNormalizeValidationCommandKey:
    """Normalization produces stable keys for lookup."""

    def test_basic(self):
        assert normalize_validation_command_key("pytest") == "pytest"

    def test_collapses_whitespace(self):
        assert (
            normalize_validation_command_key("  pytest   tests/  ")
            == "pytest tests/"
        )

    def test_with_cwd(self):
        assert (
            normalize_validation_command_key("pytest", cwd="/project")
            == "pytest|cwd=/project"
        )

    def test_empty_command(self):
        assert normalize_validation_command_key("") == ""

    def test_cwd_without_command(self):
        assert normalize_validation_command_key("", cwd="/x") == "|cwd=/x"
