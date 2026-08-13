from __future__ import annotations

import enum
from typing import Any

__all__ = [
    "ExecutionOutcomeStatus",
    "normalize_outcome_status",
]


class ExecutionOutcomeStatus(str, enum.Enum):
    """Outcome classification for a production execution result."""

    completed = "completed"
    """Execution finished successfully with all goals met."""

    completed_with_caveats = "completed_with_caveats"
    """Execution finished but attached caveats or non-blocking concerns."""

    validation_failed = "validation_failed"
    """Production changes failed validation checks."""

    edit_mechanics_blocked = "edit_mechanics_blocked"
    """Execution could not apply edits due to mechanical tool failures."""

    scope_mismatch = "scope_mismatch"
    """Execution determined the request was out of scope or unclear."""

    needs_followup = "needs_followup"
    """Execution stopped before completion and needs another harness turn."""

    approval_rejected = "approval_rejected"
    """User rejected a required approval request."""

    cancelled = "cancelled"
    """Execution was cancelled before completion."""

    harness_error = "harness_error"
    """An unexpected error occurred in the execution harness."""


def normalize_outcome_status(value: Any) -> str | None:
    """Return a valid ExecutionOutcomeStatus string, or None for unknown values."""
    if value is None:
        return None
    if isinstance(value, ExecutionOutcomeStatus):
        return value.value
    try:
        return ExecutionOutcomeStatus(str(value).strip()).value
    except ValueError:
        return None
