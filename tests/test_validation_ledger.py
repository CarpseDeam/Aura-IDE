from aura.conversation.validation_ledger import ExecutionValidationLedger


def test_execution_validation_ledger_records_validation_evidence_passively() -> None:
    ledger = ExecutionValidationLedger()
    ledger.observe(
        {
            "command": "pytest",
            "ok": True,
            "validation_classification": "passed",
            "counts_as_validation": True,
        }
    )

    assert len(ledger) == 1
    assert ledger.records[0].ok is True


def test_execution_validation_ledger_ignores_non_validation_commands() -> None:
    ledger = ExecutionValidationLedger()
    ledger.observe({"command": "git status", "ok": True})

    assert ledger.records == []
