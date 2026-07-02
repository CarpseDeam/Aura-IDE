"""Canonical view-state labels for dispatch results."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# WorkerSummaryCard header labels
# ---------------------------------------------------------------------------


def worker_summary_status_label(
    status: str | None,
    ok: bool,
    needs_followup: bool = False,
    summary: str = "",
    *,
    is_internal: bool = False,
) -> tuple[str, str]:
    """Return ``(label_text, color)`` for a WorkerSummaryCard status header."""
    from aura.conversation.worker_outcome import WorkerOutcomeStatus
    from aura.gui.theme import DANGER, FG_MUTED, SUCCESS, WARN

    if is_internal:
        if status == WorkerOutcomeStatus.completed.value:
            return ("Done", SUCCESS)
        if status == WorkerOutcomeStatus.completed_with_caveats.value:
            return ("Done", SUCCESS)
        if ok:
            return ("Done", SUCCESS)
        return ("Needs attention", FG_MUTED)

    if status is not None:
        mapping = {
            WorkerOutcomeStatus.completed.value: ("Worker Report", SUCCESS),
            WorkerOutcomeStatus.completed_with_caveats.value: ("Worker Report", SUCCESS),
            WorkerOutcomeStatus.validation_failed.value: ("Worker Report", WARN),
            WorkerOutcomeStatus.edit_mechanics_blocked.value: ("Worker Report", WARN),
            WorkerOutcomeStatus.scope_mismatch.value: ("Worker Report", WARN),
            WorkerOutcomeStatus.approval_rejected.value: ("Changes rejected", DANGER),
            WorkerOutcomeStatus.cancelled.value: ("Cancelled", "#6b7280"),
            WorkerOutcomeStatus.harness_error.value: ("Worker Error", DANGER),
        }
        return mapping.get(status, ("Worker Report", FG_MUTED))

    # Fallback to legacy inference
    if "Waiting for approval" in summary:
        return "Waiting for approval", WARN
    if "Repairing patch" in summary:
        return "Repairing patch", WARN
    if ok:
        return ("Worker Report", SUCCESS)
    if needs_followup:
        return ("Worker Report", FG_MUTED)
    return ("Worker Report", DANGER)


def _resolve_needs_followup_label(
    ok: bool,
    summary: str,
    *,
    is_internal: bool = False,
) -> tuple[str, str]:
    """Resolve a ``needs_followup`` status into a user-facing label.

    Non-internal cases get neutral labels; receipt parsing is handled by
    WorkerSummaryCard.update_summary().
    """
    from aura.gui.theme import FG_MUTED, SUCCESS

    if is_internal:
        return ("Needs attention", FG_MUTED)
    if ok:
        return ("Completed", SUCCESS)
    return ("Needs attention", FG_MUTED)


# ---------------------------------------------------------------------------
# SpecCard finished / replay labels
# ---------------------------------------------------------------------------


def spec_finished_label(
    ok: bool,
    status: str | None = None,
    *,
    is_internal: bool = False,
) -> tuple[str, str]:
    """Return ``(label_text, color)`` for SpecCard.worker_finished()."""
    from aura.conversation.worker_outcome import WorkerOutcomeStatus, normalize_outcome_status
    from aura.gui.theme import DANGER, SUCCESS, WARN

    if is_internal:
        if status is not None:
            normalized = normalize_outcome_status(status)
            if normalized in (
                WorkerOutcomeStatus.completed.value,
                WorkerOutcomeStatus.completed_with_caveats.value,
            ):
                return ("Completed", SUCCESS)
        return ("Needs attention", WARN) if not ok else ("Completed", SUCCESS)

    # ---- non-internal: honest terminal labels ----------------------------
    if status is not None:
        mapping = {
            WorkerOutcomeStatus.completed.value: ("Completed", SUCCESS),
            WorkerOutcomeStatus.completed_with_caveats.value: ("Completed", SUCCESS),
            WorkerOutcomeStatus.validation_failed.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.edit_mechanics_blocked.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.scope_mismatch.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.approval_rejected.value: ("Failed", DANGER),
            WorkerOutcomeStatus.cancelled.value: ("Cancelled", DANGER),
            WorkerOutcomeStatus.harness_error.value: ("Failed", DANGER),
        }
        normalized = normalize_outcome_status(status)
        if normalized in mapping:
            return mapping[normalized]
    return ("Completed", SUCCESS) if ok else ("Needs attention", WARN)


def spec_replay_finished_label(
    ok: bool,
    *,
    is_internal: bool = False,
) -> tuple[str, str]:
    """Return ``(label_text, color)`` for SpecCard.set_dispatched_and_finished()
    (history replay path).

    Internal continuations must never show process ceremony.
    """
    from aura.gui.theme import DANGER, SUCCESS, WARN

    if is_internal:
        return ("Needs attention", WARN) if not ok else ("Completed", SUCCESS)
    return ("Completed", SUCCESS) if ok else ("Failed", DANGER)


# ---------------------------------------------------------------------------
# MismatchResolutionCard visibility & labels
# ---------------------------------------------------------------------------


def mismatch_card_should_show(
    *,
    is_internal: bool = False,
    suppressed: bool = False,
    has_mismatch_data: bool = False,
) -> bool:
    """Return True when a MismatchResolutionCard should appear in the chat."""
    if is_internal:
        return False
    if suppressed:
        return False
    return bool(has_mismatch_data)


def mismatch_card_labels(
    *,
    is_internal: bool = False,
) -> tuple[str, str]:
    """Return ``(title, status_line)`` for MismatchResolutionCard.

    Internal continuations get neutral labels (though the card should
    normally not be shown at all for internal cases).
    """
    if is_internal:
        return ("Needs attention", "")
    return ("Needs attention", "")
