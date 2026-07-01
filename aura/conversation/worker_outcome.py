from __future__ import annotations

import enum
from typing import Any

__all__ = [
    "WorkerOutcomeStatus",
    "normalize_outcome_status",
]


class WorkerOutcomeStatus(str, enum.Enum):
    """Outcome classification for a worker dispatch result."""

    completed = "completed"
    """Worker finished successfully with all goals met."""

    completed_with_caveats = "completed_with_caveats"
    """Worker finished but attached caveats or non-blocking concerns."""

    needs_followup = "needs_followup"
    """Worker made partial progress; a follow-up dispatch is needed."""

    needs_planner_resolution = "needs_planner_resolution"
    """Worker encountered Planner handoff conflicts with repo reality."""

    validation_failed = "validation_failed"
    """Worker-produced code failed validation checks."""

    edit_mechanics_blocked = "edit_mechanics_blocked"
    """Worker could not apply edits due to mechanical tool failures."""

    scope_mismatch = "scope_mismatch"
    """Worker determined the request was out of scope or unclear."""

    approval_rejected = "approval_rejected"
    """User rejected the dispatch approval request."""

    cancelled = "cancelled"
    """Worker execution was cancelled before completion."""

    harness_error = "harness_error"
    """An unexpected error occurred in the worker harness."""


def normalize_outcome_status(value: Any) -> str | None:
    """Return a valid WorkerOutcomeStatus string, or None for unknown values."""
    if value is None:
        return None
    if isinstance(value, WorkerOutcomeStatus):
        return value.value
    try:
        return WorkerOutcomeStatus(str(value).strip()).value
    except ValueError:
        return None
