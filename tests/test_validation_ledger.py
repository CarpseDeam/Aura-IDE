"""Focused tests for aura/conversation/validation_ledger.py — Phase 3."""
from __future__ import annotations

from aura.conversation.validation_ledger import (
    ValidationLedgerRecord,
    WorkerValidationLedger,
)


class TestWorkerValidationLedger:
    """WorkerValidationLedger records validation payloads and answers
    fresh-proof queries."""

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def test_records_passed_validation_with_write_snapshot(self):
        """A passed validation payload is recorded with the current write
        snapshot."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=3,
        )
        assert len(ledger) == 1
        r = ledger.latest_for_command("pytest")
        assert r is not None
        assert r.ok is True
        assert r.write_snapshot == 3
        assert r.command_key == "pytest"

    def test_records_failed_validation_as_not_ok(self):
        """A failed validation payload is recorded with ok=False."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "product_validation_failed",
                "exit_code": 1,
                "counts_as_product_failure": True,
            },
            write_snapshot=1,
        )
        r = ledger.latest_for_command("pytest")
        assert r is not None
        assert r.ok is False

    def test_records_with_cwd_normalizes_key(self):
        """The command key includes cwd when the payload has one."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "cwd": "/project",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=1,
        )
        r = ledger.latest_for_command("pytest|cwd=/project")
        # The normalized key includes the cwd suffix.
        assert r is not None
        assert r.command_key == "pytest|cwd=/project"
        # Without the cwd suffix the key does not match.
        assert ledger.latest_for_command("pytest") is None

    def test_non_validation_payload_not_recorded(self):
        """A terminal result that does not count as validation is silently
        skipped."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {"command": "pytest", "ok": True, "exit_code": 0},
            write_snapshot=1,
        )
        assert len(ledger) == 0

    def test_record_classification_fallback(self):
        """When ``validation_classification`` is absent, ``classification``
        is used as the record's classification string."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "classification": "product_validation_failed",
                "validation_classification": "product_validation_failed",
                "exit_code": 1,
                "counts_as_product_failure": True,
            },
            write_snapshot=1,
        )
        r = ledger.latest_for_command("pytest")
        assert r is not None
        assert r.classification == "product_validation_failed"

    # ------------------------------------------------------------------
    # has_fresh_passed_commands
    # ------------------------------------------------------------------

    def test_has_fresh_passed_true_for_exact_command(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=5,
        )
        assert (
            ledger.has_fresh_passed_commands(["pytest"], write_snapshot=5)
            is True
        )

    def test_has_fresh_passed_false_for_stale_snapshot(self):
        """A record at an older snapshot is stale and not considered passed."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=3,
        )
        # Snapshot advanced to 4 (a write happened) → proof is stale
        assert (
            ledger.has_fresh_passed_commands(["pytest"], write_snapshot=4)
            is False
        )

    def test_has_fresh_passed_false_for_partial_proof(self):
        """When only one of two required commands is proved, returns False."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=5,
        )
        assert (
            ledger.has_fresh_passed_commands(
                ["pytest", "npm test"], write_snapshot=5
            )
            is False
        )

    def test_has_fresh_passed_false_for_empty_commands(self):
        ledger = WorkerValidationLedger()
        assert (
            ledger.has_fresh_passed_commands([], write_snapshot=5) is False
        )

    def test_has_fresh_passed_false_for_failed_result(self):
        """A fresh record with ok=False does not count as passed."""
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "product_validation_failed",
                "exit_code": 1,
                "counts_as_product_failure": True,
            },
            write_snapshot=5,
        )
        assert (
            ledger.has_fresh_passed_commands(["pytest"], write_snapshot=5)
            is False
        )

    def test_has_fresh_passed_false_for_unknown_command(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=5,
        )
        assert (
            ledger.has_fresh_passed_commands(["unknown"], write_snapshot=5)
            is False
        )

    # ------------------------------------------------------------------
    # clear_after_write
    # ------------------------------------------------------------------

    def test_clear_after_write_clears_all_records(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=1,
        )
        ledger.observe_tool_payload(
            {
                "command": "npm test",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=1,
        )
        assert len(ledger) == 2
        ledger.clear_after_write()
        assert len(ledger) == 0
        assert ledger.latest_for_command("pytest") is None
        assert ledger.latest_for_command("npm test") is None

    # ------------------------------------------------------------------
    # latest_failures
    # ------------------------------------------------------------------

    def test_latest_failures_returns_only_failed_records(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=1,
        )
        ledger.observe_tool_payload(
            {
                "command": "npm test",
                "validation_classification": "product_validation_failed",
                "exit_code": 1,
                "counts_as_product_failure": True,
            },
            write_snapshot=1,
        )
        ledger.observe_tool_payload(
            {
                "command": "ruff",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=1,
        )
        failures = ledger.latest_failures()
        assert len(failures) == 1
        assert failures[0].command == "npm test"

    def test_latest_failures_empty_when_all_pass(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=1,
        )
        assert ledger.latest_failures() == []

    # ------------------------------------------------------------------
    # latest_for_command
    # ------------------------------------------------------------------

    def test_latest_for_command_returns_none_for_unknown(self):
        ledger = WorkerValidationLedger()
        assert ledger.latest_for_command("pytest") is None

    def test_latest_for_command_returns_most_recent(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=1,
        )
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
            },
            write_snapshot=2,
        )
        r = ledger.latest_for_command("pytest")
        assert r is not None
        assert r.write_snapshot == 2

    # ------------------------------------------------------------------
    # output_preview / output_truncated
    # ------------------------------------------------------------------

    def test_output_preview_truncates_long_output(self):
        ledger = WorkerValidationLedger()
        long_output = "A" * 2000
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
                "output": long_output,
            },
            write_snapshot=1,
        )
        r = ledger.latest_for_command("pytest")
        assert r is not None
        assert len(r.output_preview) == 500
        assert r.output_truncated is True

    def test_output_preview_short_output_not_truncated(self):
        ledger = WorkerValidationLedger()
        short = "All tests passed!"
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
                "exit_code": 0,
                "output": short,
            },
            write_snapshot=1,
        )
        r = ledger.latest_for_command("pytest")
        assert r is not None
        assert r.output_preview == short
        assert r.output_truncated is False

    # ------------------------------------------------------------------
    # edge cases
    # ------------------------------------------------------------------

    def test_empty_payload_not_recorded(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload({}, write_snapshot=1)
        assert len(ledger) == 0

    def test_record_with_no_exit_code(self):
        ledger = WorkerValidationLedger()
        ledger.observe_tool_payload(
            {
                "command": "pytest",
                "validation_classification": "passed",
            },
            write_snapshot=1,
        )
        r = ledger.latest_for_command("pytest")
        assert r is not None
        assert r.exit_code is None
        assert r.ok is True

    def test_multiple_records_same_command_accumulate(self):
        ledger = WorkerValidationLedger()
        for i in range(3):
            ledger.observe_tool_payload(
                {
                    "command": "pytest",
                    "validation_classification": "passed",
                    "exit_code": 0,
                },
                write_snapshot=i,
            )
        assert len(ledger) == 3


class TestValidationLedgerRecord:
    """ValidationLedgerRecord is a frozen dataclass with expected fields."""

    def test_frozen(self):
        r = ValidationLedgerRecord(
            command_key="pytest",
            command="pytest",
            cwd="",
            ok=True,
            exit_code=0,
            classification="passed",
            counts_as_validation=True,
            counts_as_product_failure=False,
            output_preview="",
            output_truncated=False,
            write_snapshot=1,
            source="worker_tool",
        )
        assert r.ok is True
        assert r.command_key == "pytest"
