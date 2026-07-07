"""Single proof ledger for Worker-run validation results.

Records validation payloads as they are observed during tool execution.
The ledger is the source of truth for ``has_fresh_passed_commands`` checks
used by finalization to skip duplicate reruns when proof is fresh.
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
    """Immutable record of one observed validation attempt.

    Every field is populated at creation time from the terminal payload
    and the write snapshot supplied by the caller.
    """

    command_key: str
    """Normalized lookup key (see ``normalize_validation_command_key``)."""
    command: str
    """Raw command string from the payload."""
    cwd: str
    """Working directory from the payload, if any."""
    ok: bool
    """Whether the validation genuinely passed (uses truth helper)."""
    exit_code: int | None
    """Exit code from the terminal result."""
    classification: str
    """Validation classification string, e.g. ``"passed"``."""
    counts_as_validation: bool
    """Whether the payload counts as a validation attempt."""
    counts_as_product_failure: bool
    """Whether the payload is a product (code) failure."""
    output_preview: str
    """First 500 characters of the terminal output."""
    output_truncated: bool
    """Whether the original output exceeded the preview limit."""
    write_snapshot: int
    """Cumulative write-attempt count at the time of observation."""
    source: str
    """Origin label, e.g. ``"worker_tool"`` or ``"final_explicit"``."""


class WorkerValidationLedger:
    """Tracks validation payloads across tool rounds.

    Only records payloads that ``validation_payload_counts_as_validation``
    considers to be a validation attempt.  Non-validation terminal results
    are silently ignored.

    The ledger is append-only: records accumulate in observation order.
    ``clear_after_write`` removes all records when a write invalidates
    prior proof.
    """

    def __init__(self) -> None:
        self._records: list[ValidationLedgerRecord] = []

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def observe_tool_payload(
        self,
        payload: dict[str, Any],
        write_snapshot: int,
        source: str = "worker_tool",
    ) -> None:
        """Record *payload* when it counts as a validation attempt.

        Only payloads that ``validation_payload_counts_as_validation``
        returns ``True`` for are stored.  Others are silently ignored.
        """
        if not validation_payload_counts_as_validation(payload):
            return

        command = str(payload.get("command") or "")
        cwd = str(payload.get("cwd") or "")
        raw_output = str(
            payload.get("output") or payload.get("output_preview") or ""
        )
        preview = raw_output[:500]

        record = ValidationLedgerRecord(
            command_key=normalize_validation_command_key(command, cwd=cwd),
            command=command,
            cwd=cwd,
            ok=validation_payload_passed(payload),
            exit_code=payload.get("exit_code"),
            classification=str(
                payload.get("validation_classification")
                or payload.get("classification")
                or ""
            ),
            counts_as_validation=True,
            counts_as_product_failure=bool(payload.get("counts_as_product_failure")),
            output_preview=preview,
            output_truncated=len(raw_output) > 500,
            write_snapshot=write_snapshot,
            source=source,
        )
        self._records.append(record)

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def clear_after_write(self) -> None:
        """Remove all records — a write invalidates prior proof."""
        self._records.clear()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def has_fresh_passed_commands(
        self,
        commands: list[str],
        write_snapshot: int,
    ) -> bool:
        """Return ``True`` when every command in *commands* has fresh passed proof.

        A record is *fresh* when its ``write_snapshot`` equals the current
        *write_snapshot*.  A command with a stale record (snapshot below
        current) or no record at all is treated as *not* having fresh passed
        proof.

        Returns ``False`` when *commands* is empty.
        """
        if not commands:
            return False
        for cmd in commands:
            if not self._has_fresh_passed(cmd, write_snapshot):
                return False
        return True

    def _has_fresh_passed(
        self,
        command: str,
        write_snapshot: int,
    ) -> bool:
        """Check a single command for fresh passed proof.

        Iterates records in reverse (most-recent first).  Returns ``True``
        when the most recent record matching *command* has
        ``ok is True`` and ``write_snapshot == write_snapshot``.
        Returns ``False`` when there is no matching record, or the
        most recent matching record is stale (snapshot < current), or
        it has a fresh snapshot but ``ok is False``.
        """
        for r in reversed(self._records):
            if r.command_key != command:
                continue
            if r.write_snapshot == write_snapshot:
                return r.ok
            # Stale record (snapshot < current) → no fresh proof.
            return False
        return False

    def latest_for_command(
        self,
        command: str,
    ) -> ValidationLedgerRecord | None:
        """Return the most recent record for *command*, or ``None``."""
        for r in reversed(self._records):
            if r.command_key == command:
                return r
        return None

    def latest_failures(self) -> list[ValidationLedgerRecord]:
        """Return all records where the validation did not pass."""
        return [r for r in self._records if not r.ok]

    def __len__(self) -> int:
        return len(self._records)


__all__ = [
    "ValidationLedgerRecord",
    "WorkerValidationLedger",
]
