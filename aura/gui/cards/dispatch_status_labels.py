"""Canonical view-state labels for dispatch results.

Every GUI component that renders a dispatch outcome label routes through
the functions in this module so internal continuation never leaks
user-facing failure, mismatch, or blocker language.

Internal continuations show: *Retrying*, *Working*, or nothing.
Terminal / user-visible cases remain honest.
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

    Internal continuations must never show "Planner resolving mismatch",
    "Failed", or any other blocker language — they get neutral labels.
    """
    from aura.conversation.dispatch import WorkerOutcomeStatus
    from aura.gui.theme import DANGER, FG_MUTED, SUCCESS, WARN

    if is_internal:
        # Internal continuation — never show failure/blocker labels
        if status == WorkerOutcomeStatus.needs_planner_resolution.value:
            return ("Retrying", WARN)
        if status == WorkerOutcomeStatus.needs_followup.value:
            return _resolve_needs_followup_label(ok, summary, is_internal=True)
        if status == WorkerOutcomeStatus.completed.value:
            return ("Done", SUCCESS)
        if status == WorkerOutcomeStatus.completed_with_caveats.value:
            return ("Done", SUCCESS)
        if ok:
            return ("Done", SUCCESS)
        return ("Working", FG_MUTED)

    # ---- non-internal: keep terminal labels honest -----------------------
    if status is not None:
        # needs_followup is internal Planner continuation machinery.
        # Never show a scary yellow card for it — resolve contextually
        # from the summary receipt instead.
        if status == WorkerOutcomeStatus.needs_followup.value:
            return _resolve_needs_followup_label(ok, summary, is_internal=False)

        mapping = {
            WorkerOutcomeStatus.completed.value: ("Done", SUCCESS),
            WorkerOutcomeStatus.completed_with_caveats.value: ("Done", SUCCESS),
            WorkerOutcomeStatus.validation_failed.value: ("Failed validation", DANGER),
            WorkerOutcomeStatus.edit_mechanics_blocked.value: ("Edit mechanics blocked", WARN),
            WorkerOutcomeStatus.scope_mismatch.value: ("Scope mismatch", WARN),
            WorkerOutcomeStatus.approval_rejected.value: ("Approval rejected", DANGER),
            WorkerOutcomeStatus.cancelled.value: ("Cancelled", "#6b7280"),
            WorkerOutcomeStatus.harness_error.value: ("Harness error", DANGER),
            WorkerOutcomeStatus.needs_planner_resolution.value: ("Retrying", WARN),
        }
        return mapping.get(status, ("Unknown", "#6b7280"))

    # Fallback to legacy inference
    if "Waiting for approval" in summary:
        return "Waiting for approval", WARN
    if "Repairing patch" in summary:
        return "Repairing patch", WARN
    if ok:
        return ("Done", SUCCESS)
    if summary.startswith("Harness error"):
        return ("Harness error", DANGER)
    if summary.startswith("Validation failed"):
        return ("Failed validation", WARN)
    # needs_followup without explicit status — neutral, not scary
    return ("Details in Worker Log", FG_MUTED)


def _resolve_needs_followup_label(
    ok: bool,
    summary: str,
    *,
    is_internal: bool = False,
) -> tuple[str, str]:
    """Resolve a ``needs_followup`` status into a user-facing label.

    Internal continuations get a compact *Retrying* label.
    Non-internal cases get neutral labels — the full receipt parsing
    for display is handled by WorkerSummaryCard.update_summary().
    """
    from aura.gui.theme import FG_MUTED, SUCCESS, WARN

    if is_internal:
        return ("Retrying", WARN)
    if ok:
        return ("Completed", SUCCESS)
    return ("Details in Worker Log", FG_MUTED)


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

    Internal continuations must never show failure/error language.
    """
    from aura.conversation.dispatch import WorkerOutcomeStatus, normalize_outcome_status
    from aura.gui.theme import DANGER, SUCCESS, WARN

    if is_internal:
        # Internal continuation — neutral retry / working labels only
        if status is not None:
            normalized = normalize_outcome_status(status)
            if normalized == WorkerOutcomeStatus.needs_planner_resolution.value:
                return ("Retrying", WARN)
            if normalized == WorkerOutcomeStatus.needs_followup.value:
                return ("Retrying", WARN)
            if normalized in (
                WorkerOutcomeStatus.completed.value,
                WorkerOutcomeStatus.completed_with_caveats.value,
            ):
                return ("Completed", SUCCESS)
        return ("Retrying", WARN) if not ok else ("Completed", SUCCESS)

    # ---- non-internal: honest terminal labels ----------------------------
    if status is not None:
        mapping = {
            WorkerOutcomeStatus.completed.value: ("Completed", SUCCESS),
            WorkerOutcomeStatus.completed_with_caveats.value: ("Completed with caveats", WARN),
            WorkerOutcomeStatus.needs_followup.value: ("Completed with caveats", WARN),
            WorkerOutcomeStatus.validation_failed.value: ("Validation failed", DANGER),
            WorkerOutcomeStatus.edit_mechanics_blocked.value: ("Edit mechanics blocked", WARN),
            WorkerOutcomeStatus.scope_mismatch.value: ("Scope mismatch", WARN),
            WorkerOutcomeStatus.approval_rejected.value: ("Approval rejected", DANGER),
            WorkerOutcomeStatus.cancelled.value: ("Cancelled", DANGER),
            WorkerOutcomeStatus.harness_error.value: ("Harness error", DANGER),
            WorkerOutcomeStatus.needs_planner_resolution.value: ("Retrying", WARN),
        }
        normalized = normalize_outcome_status(status)
        if normalized in mapping:
            return mapping[normalized]
    return ("Completed", SUCCESS) if ok else ("Blocked", WARN)


def spec_replay_finished_label(
    ok: bool,
    *,
    is_internal: bool = False,
) -> tuple[str, str]:
    """Return ``(label_text, color)`` for SpecCard.set_dispatched_and_finished()
    (history replay path).

    Internal continuations must never show "Completed with errors".
    """
    from aura.gui.theme import DANGER, SUCCESS, WARN

    if is_internal:
        return ("Retrying", WARN) if not ok else ("Completed", SUCCESS)
    return ("Completed", SUCCESS) if ok else ("Blocked", DANGER)


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
        return ("Retrying", "Coordinating with workspace…")
    return ("Resolving plan differences", "Coordinating next steps…")
