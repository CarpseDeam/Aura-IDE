"""Focused tests for aura/conversation/validation_ledger.py — simplified passive."""
from __future__ import annotations

from aura.conversation.validation_ledger import (
    ValidationLedgerRecord,
    WorkerValidationLedger,
)


class TestWorkerValidationLedger:
    """WorkerValidationLedger records validation payloads for passive display."""

    def test_records_passed_validation(self):
        """A passed validation payload is recorded."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "pytest",
            "ok": True,
            "validation_classification": "passed",
            "counts_as_validation": True,
        })
        assert len(ledger) == 1
        r = ledger.records[0]
        assert r.ok is True
        assert r.command_key == "pytest"
        assert r.classification == "passed"

    def test_records_failed_validation_as_not_ok(self):
        """A failed validation payload is recorded with ok=False."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "pytest",
            "ok": False,
            "validation_classification": "failed",
            "counts_as_validation": True,
        })
        assert len(ledger) == 1
        r = ledger.records[0]
        assert r.ok is False

    def test_skips_non_validation_payloads(self):
        """Payloads without validation indicators are silently ignored."""
        ledger = WorkerValidationLedger()
        ledger.observe({"command": "ls", "ok": True})
        assert len(ledger) == 0

    def test_auto_validation_is_recorded(self):
        """Payloads with auto_validation=True are recorded."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "python -m compileall src/",
            "ok": True,
            "auto_validation": True,
        })
        assert len(ledger) == 1
        # Without validation_classification, ok defaults to False in the ledger
        # (validation_payload_passed requires explicit "passed" classification)
        assert ledger.records[0].ok is False

    def test_records_are_independent(self):
        """Multiple observations produce independent records."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "pytest", "ok": True,
            "validation_classification": "passed", "counts_as_validation": True,
        })
        ledger.observe({
            "command": "ruff check", "ok": False,
            "validation_classification": "failed", "counts_as_validation": True,
        })
        assert len(ledger) == 2
        assert ledger.records[0].command == "pytest"
        assert ledger.records[1].command == "ruff check"

    def test_records_property_returns_copy(self):
        """The records property returns a copy, not the internal list."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "pytest", "ok": True,
            "counts_as_validation": True,
        })
        r = ledger.records
        r.clear()
        assert len(ledger) == 1

    def test_output_truncation_not_stored(self):
        """Simplified ledger does not store output preview or truncation."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "pytest", "ok": True,
            "counts_as_validation": True,
        })
        r = ledger.records[0]
        assert not hasattr(r, "output_preview")
        assert not hasattr(r, "output_truncated")

    def test_no_write_snapshot(self):
        """Simplified ledger does not store write_snapshot."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "pytest", "ok": True,
            "counts_as_validation": True,
        })
        r = ledger.records[0]
        assert not hasattr(r, "write_snapshot")

    def test_no_source_field(self):
        """Simplified ledger does not store source."""
        ledger = WorkerValidationLedger()
        ledger.observe({
            "command": "pytest", "ok": True,
            "counts_as_validation": True,
        })
        r = ledger.records[0]
        assert not hasattr(r, "source")

    def test_len_zero_for_empty_ledger(self):
        """Empty ledger has length 0."""
        ledger = WorkerValidationLedger()
        assert len(ledger) == 0

    def test_has_fresh_passed_not_available(self):
        """has_fresh_passed_commands is removed from the simplified ledger."""
        ledger = WorkerValidationLedger()
        assert not hasattr(ledger, "has_fresh_passed_commands")

    def test_latest_for_command_not_available(self):
        """latest_for_command is removed from the simplified ledger."""
        ledger = WorkerValidationLedger()
        assert not hasattr(ledger, "latest_for_command")

    def test_latest_failures_not_available(self):
        """latest_failures is removed from the simplified ledger."""
        ledger = WorkerValidationLedger()
        assert not hasattr(ledger, "latest_failures")
