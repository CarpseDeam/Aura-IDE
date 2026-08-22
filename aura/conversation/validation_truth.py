"""Pure helper functions for determining validation truth from raw payloads.

These are the single source of truth for whether a validation attempt
genuinely passed or failed.  No other module should re-implement this logic.

Terminal execution success (``ok=True``) is *never* validation success on its
own — a command can run successfully but produce a failing validation result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def validation_payload_counts_as_validation(payload: dict[str, Any]) -> bool:
    """Return True when *payload* represents a validation attempt.

    Checks the structured validation fields on the payload, plus the
    ``auto_validation`` flag carried from the terminal tracker.
    """
    if payload.get("counts_as_validation") is True:
        return True
    if payload.get("auto_validation") is True:
        return True
    vc = str(payload.get("validation_classification") or "")
    if vc:
        return True
    return False


def validation_payload_passed(payload: dict[str, Any]) -> bool:
    """Return True when the validation genuinely passed.

    Truth rules (in order of precedence):

    1. If ``validation_payload_failed(payload)`` is True → not passed (fail
       closed: contradiction fields like ``exit_code != 0`` beat a pass label).
    2. ``validation_classification == "passed"`` → passed.
    3. ``command_outcome_classification == "passed"`` → passed only when the
       payload also counts as a validation attempt.  This field is set by
       the tool runner for *every* command outcome, not just validation,
       so the validation context guard is required.
    4. ``classification == "passed"`` → passed only when the payload
       also counts as a validation attempt.
    5. Every other case → not passed, even when ``ok=True``.
    """
    # Fail-first guard: any failure indicator overrides a pass label.
    if validation_payload_failed(payload):
        return False
    vc = str(payload.get("validation_classification") or "")
    if vc == "passed":
        return True
    coc = str(payload.get("command_outcome_classification") or "")
    if coc == "passed":
        return validation_payload_counts_as_validation(payload)
    c = str(payload.get("classification") or "")
    if c == "passed":
        return validation_payload_counts_as_validation(payload)
    return False


def validation_payload_failed(payload: dict[str, Any]) -> bool:
    """Return True when the validation failed for *any* reason.

    Failing means the validation attempted to run but produced a failure
    outcome — product failure, infra failure, timeout, etc.

    Truth rules:

    1. ``counts_as_product_failure=True`` → failed.
    2. ``exit_code is not None and exit_code != 0`` → failed.
    3. ``command_success=False`` → failed.
    4. Otherwise → not failed from the payload's own fields.
    """
    if payload.get("counts_as_product_failure") is True:
        return True
    exit_code = payload.get("exit_code")
    if exit_code is not None and exit_code != 0:
        return True
    if payload.get("command_success") is False:
        return True
    return False


def validation_payload_product_failure(payload: dict[str, Any]) -> bool:
    """Return True when the validation failed as a product (code) issue."""
    return payload.get("counts_as_product_failure") is True


def normalize_validation_command_key(command: str, cwd: str = "") -> str:
    """Normalize a validation command into a stable lookup key.

    Strips leading/trailing whitespace, collapses multiple spaces, and
    appends a ``|cwd=<cwd>`` suffix when *cwd* is non-empty.
    """
    key = " ".join(str(command or "").strip().split())
    cwd_str = str(cwd or "").strip()
    if cwd_str:
        key = f"{key}|cwd={cwd_str}"
    return key


def _record_passed(record: dict[str, Any]) -> bool:
    if "validation_ok" in record:
        return bool(record["validation_ok"])
    return validation_payload_passed(record)


def _record_failed(record: dict[str, Any]) -> bool:
    if "validation_ok" in record:
        return not bool(record["validation_ok"])
    return validation_payload_failed(record)


@dataclass(frozen=True)
class ValidationOutcome:
    """Per-command validation history for a run: pass/fail with repair history."""

    command: str
    attempts: int
    passed: bool
    repaired: bool  # failed at least once, then passed
    last_exit_code: Any = None


def summarize_validation(
    validation_results: list[dict[str, Any]],
) -> list[ValidationOutcome]:
    """Collapse a validation ledger into one outcome per normalized command.

    Preserves the failure->repair->rerun story: a command that failed and
    later passed is reported with ``repaired=True``.
    """
    ordered: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in validation_results:
        command = " ".join(str(record.get("command") or "").split())
        if command not in grouped:
            grouped[command] = []
            ordered.append(command)
        grouped[command].append(record)

    outcomes: list[ValidationOutcome] = []
    for command in ordered:
        records = grouped[command]
        any_failed = any(_record_failed(record) for record in records)
        final_passed = _record_passed(records[-1])
        outcomes.append(
            ValidationOutcome(
                command=command,
                attempts=len(records),
                passed=final_passed,
                repaired=bool(any_failed and final_passed),
                last_exit_code=records[-1].get("exit_code"),
            )
        )
    return outcomes


__all__ = [
    "ValidationOutcome",
    "normalize_validation_command_key",
    "summarize_validation",
    "validation_payload_counts_as_validation",
    "validation_payload_failed",
    "validation_payload_passed",
    "validation_payload_product_failure",
]
