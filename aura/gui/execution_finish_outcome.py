"""Execution finish outcome classification for GUI presentation."""
from __future__ import annotations

from dataclasses import dataclass

#: Truthful terminal status labels shared by the workspace status chip and
#: any chat-side surfacing of a non-success outcome. Named rather than
#: spelled inline so both call sites can never drift apart on wording.
STATUS_DONE = "Done"
STATUS_NEEDS_FOLLOWUP = "Needs follow-up"
STATUS_CANCELLED = "Cancelled"
STATUS_ERROR = "Error"


def terminal_status_label(*, ok: bool, needs_followup: bool, status: str | None) -> str:
    """Return the truthful terminal status label for a finished/cancelled run.

    ``status`` carries the backend's ``ExecutionOutcomeStatus`` (or ``None``);
    only ``cancelled`` gets a dedicated label here since it is the one status
    that means something distinct from the ok/needs_followup pair alone.
    Every other status (validation_failed, edit_mechanics_blocked,
    scope_mismatch, approval_rejected, harness_error, completed*) is already
    fully described by whether the run finished ok and whether it still
    needs another turn.
    """
    from aura.conversation.execution_outcome import ExecutionOutcomeStatus, normalize_outcome_status

    normalized = normalize_outcome_status(status)
    if normalized == ExecutionOutcomeStatus.cancelled.value:
        return STATUS_CANCELLED
    if needs_followup:
        return STATUS_NEEDS_FOLLOWUP
    if ok:
        return STATUS_DONE
    return STATUS_ERROR


@dataclass(frozen=True)
class ExecutionFinishOutcome:
    metadata: dict
    extras: dict
    terminal_success: bool
    suppress_main_summary: bool
    status_label: str

    @property
    def should_show_visible_summary(self) -> bool:
        return not self.suppress_main_summary


def classify_execution_finish(
    *,
    ok: bool,
    needs_followup: bool,
    status: str | None,
    metadata: dict,
) -> ExecutionFinishOutcome:
    extras = metadata.get("extras") if isinstance(metadata.get("extras"), dict) else {}
    terminal_success = bool(ok and not needs_followup)
    if terminal_success:
        extras = _scrub_internal_success_extras(extras)
        metadata = {**metadata, "extras": extras}

    return ExecutionFinishOutcome(
        metadata=metadata,
        extras=extras,
        terminal_success=terminal_success,
        suppress_main_summary=False,
        status_label=terminal_status_label(ok=ok, needs_followup=needs_followup, status=status),
    )


def classify_finish_outcome(
    *,
    ok: bool,
    needs_followup: bool,
    status: str | None,
    metadata: dict,
) -> ExecutionFinishOutcome:
    return classify_execution_finish(
        ok=ok,
        needs_followup=needs_followup,
        status=status,
        metadata=metadata,
    )


def _scrub_internal_success_extras(extras: dict) -> dict:
    """Drop retry-control flags that must not survive onto a later success."""
    if not isinstance(extras, dict):
        return {}
    result = dict(extras)
    for key in (
        "mismatch_kind",
        "mismatch_question",
        "failure_constraint",
        "dispatch_spec_rejected",
    ):
        result.pop(key, None)
    return result
