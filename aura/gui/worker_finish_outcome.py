"""Worker finish outcome classification for GUI presentation."""
from __future__ import annotations

from dataclasses import dataclass

from aura.gui.cards.dispatch_status_labels import mismatch_card_should_show


@dataclass(frozen=True)
class WorkerFinishOutcome:
    metadata: dict
    extras: dict
    terminal_success: bool
    suppress_main_summary: bool
    is_mismatch: bool

    @property
    def should_clear_dispatch_card(self) -> bool:
        return True

    @property
    def should_show_visible_summary(self) -> bool:
        return not self.suppress_main_summary

    @property
    def mismatch_display(self) -> tuple[str, str]:
        return (
            str(self.extras.get("mismatch_kind", "")),
            str(self.extras.get("mismatch_question", "")),
        )


def classify_worker_finish(
    *,
    ok: bool,
    needs_followup: bool,
    status: str | None,
    metadata: dict,
) -> WorkerFinishOutcome:
    extras = metadata.get("extras") if isinstance(metadata.get("extras"), dict) else {}
    terminal_success = bool(ok and not needs_followup)
    if terminal_success:
        extras = _scrub_internal_success_extras(extras)
        metadata = {**metadata, "extras": extras}

    suppress_main_summary = False
    has_mismatch_data = bool(
        extras.get("mismatch_kind")
        or extras.get("mismatch_question")
    )
    is_mismatch = mismatch_card_should_show(
        suppressed=False,
        has_mismatch_data=has_mismatch_data,
    )
    if is_mismatch:
        suppress_main_summary = True

    return WorkerFinishOutcome(
        metadata=metadata,
        extras=extras,
        terminal_success=terminal_success,
        suppress_main_summary=suppress_main_summary,
        is_mismatch=is_mismatch,
    )


def classify_finish_outcome(
    *,
    ok: bool,
    needs_followup: bool,
    status: str | None,
    metadata: dict,
) -> WorkerFinishOutcome:
    return classify_worker_finish(
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
        "internal_planner_handoff",
        "planner_resolution_needed",
        "mismatch_kind",
        "mismatch_question",
        "failure_constraint",
        "dispatch_spec_rejected",
    ):
        result.pop(key, None)
    return result
