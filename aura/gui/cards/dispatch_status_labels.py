"""Canonical view-state labels for dispatch results.

Every GUI component that renders a dispatch outcome label routes through
the functions in this module so internal continuation never leaks
user-facing failure, mismatch, or blocker language.

Internal continuations do not show process ceremony.
Terminal / user-visible cases use compact product labels.
"""

from __future__ import annotations

from typing import Any

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
    """Return ``(label_text, color)`` for a WorkerSummaryCard status header.

    Internal continuations must never show process, mismatch, or blocker labels.
    """
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

    # ---- non-internal: keep terminal labels honest -----------------------
    if status is not None:
        # needs_followup is internal Planner continuation machinery.
        # Never show a scary yellow card for it — resolve contextually
        # from the summary receipt instead.
        if status == WorkerOutcomeStatus.needs_followup.value:
            return _resolve_needs_followup_label(ok, summary, is_internal=False)

        mapping = {
            WorkerOutcomeStatus.completed.value: ("Completed", SUCCESS),
            WorkerOutcomeStatus.completed_with_caveats.value: ("Completed", SUCCESS),
            WorkerOutcomeStatus.validation_failed.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.edit_mechanics_blocked.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.scope_mismatch.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.approval_rejected.value: ("Failed", DANGER),
            WorkerOutcomeStatus.cancelled.value: ("Cancelled", "#6b7280"),
            WorkerOutcomeStatus.harness_error.value: ("Failed", DANGER),
            WorkerOutcomeStatus.needs_planner_resolution.value: ("Needs attention", WARN),
        }
        return mapping.get(status, ("Needs attention", FG_MUTED))

    # Fallback to legacy inference
    if "Waiting for approval" in summary:
        return "Waiting for approval", WARN
    if "Repairing patch" in summary:
        return "Repairing patch", WARN
    if ok:
        return ("Completed", SUCCESS)
    if needs_followup:
        return ("Needs attention", FG_MUTED)
    return ("Failed", DANGER)


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
    from aura.gui.theme import FG_MUTED, SUCCESS, WARN

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
    """Return ``(label_text, color)`` for SpecCard.worker_finished().

    Internal continuations must never show process or error ceremony.
    """
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
            WorkerOutcomeStatus.needs_followup.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.validation_failed.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.edit_mechanics_blocked.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.scope_mismatch.value: ("Needs attention", WARN),
            WorkerOutcomeStatus.approval_rejected.value: ("Failed", DANGER),
            WorkerOutcomeStatus.cancelled.value: ("Cancelled", DANGER),
            WorkerOutcomeStatus.harness_error.value: ("Failed", DANGER),
            WorkerOutcomeStatus.needs_planner_resolution.value: ("Needs attention", WARN),
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
    """Return True when a MismatchResolutionCard should appear in the chat.

    Never shown for:
    - Internal continuations (Planner restart is invisible)
    - Suppressed follow-up cards
    - Results without mismatch kind/question data
    """
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
