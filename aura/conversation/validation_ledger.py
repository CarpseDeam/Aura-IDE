"""Passive append-only validation evidence ledger for display/metadata.

No authority is derived from this ledger.  It is an append-only log of
validation payloads observed during tool execution, carried as passive
metadata for the GUI display.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.conversation.validation_truth import (
    normalize_validation_command_key,
    validation_payload_counts_as_validation,
    validation_payload_passed,
)


@dataclass(frozen=True)
class ValidationLedgerRecord:
    """Immutable record of one observed validation attempt."""

    command_key: str
    """Normalized lookup key (see ``normalize_validation_command_key``)."""
    command: str
    """Raw command string from the payload."""
    ok: bool
    """Whether the validation genuinely passed."""
    classification: str
    """Validation classification string, e.g. ``"passed"``."""
    counts_as_validation: bool
    """Whether the payload counts as a validation attempt."""


class WorkerValidationLedger:
    """Passive append-only validation evidence log.

    Records are added via ``observe()`` and exposed via the ``records``
    property for display only.
    """

    def __init__(self) -> None:
        self._records: list[ValidationLedgerRecord] = []

    def observe(self, payload: dict[str, Any]) -> None:
        """Record *payload* when it counts as a validation attempt.

        Only payloads that ``validation_payload_counts_as_validation``
        returns ``True`` for are stored.  Others are silently ignored.
        """
        if not validation_payload_counts_as_validation(payload):
            return

        command = str(payload.get("command") or "")
        record = ValidationLedgerRecord(
            command_key=normalize_validation_command_key(command),
            command=command,
            ok=validation_payload_passed(payload),
            classification=str(
                payload.get("validation_classification")
                or payload.get("classification")
                or ""
            ),
            counts_as_validation=True,
        )
        self._records.append(record)

    @property
    def records(self) -> list[ValidationLedgerRecord]:
        """Return a copy of all records (read-only display access)."""
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)


__all__ = [
    "ValidationLedgerRecord",
    "WorkerValidationLedger",
]
