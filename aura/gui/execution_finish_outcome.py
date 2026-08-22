"""Execution finish outcome classification for GUI presentation."""
from __future__ import annotations

#: Truthful terminal status labels shared by the workspace status chip and
#: any chat-side surfacing of a non-success outcome. Named rather than
#: spelled inline so both call sites can never drift apart on wording.
STATUS_DONE = "Done"
STATUS_CANCELLED = "Cancelled"
STATUS_ERROR = "Error"


def terminal_status_label(*, ok: bool, status: str | None) -> str:
    """Return the truthful terminal status label for a finished/cancelled run.

    ``status`` carries the backend's ``ExecutionOutcomeStatus`` (or ``None``);
    only ``cancelled`` gets a dedicated label here since it is the one status
    that means something distinct from ``ok`` alone. Every other status
    (validation_failed, edit_mechanics_blocked, scope_mismatch,
    approval_rejected, harness_error, completed*) is already fully described
    by whether the run finished ok.
    """
    from aura.conversation.execution_outcome import ExecutionOutcomeStatus, normalize_outcome_status

    normalized = normalize_outcome_status(status)
    if normalized == ExecutionOutcomeStatus.cancelled.value:
        return STATUS_CANCELLED
    if ok:
        return STATUS_DONE
    return STATUS_ERROR
