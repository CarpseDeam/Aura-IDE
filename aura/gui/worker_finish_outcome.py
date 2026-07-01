"""Worker finish outcome classification for GUI presentation."""
from __future__ import annotations

from dataclasses import dataclass

from aura.conversation.dispatch_lifecycle import (
    is_internal_dispatch_continuation,
    is_user_visible_dispatch_blocker,
)
from aura.gui.cards.dispatch_status_labels import mismatch_card_should_show


@dataclass(frozen=True)
class WorkerFinishOutcome:
    metadata: dict
    extras: dict
    terminal_success: bool
    is_internal: bool
    user_visible_blocker: bool
    suppress_user_followup_card: bool
    suppress_main_summary: bool
    is_mismatch: bool

    @property
    def should_clear_dispatch_card(self) -> bool:
        return not (self.suppress_user_followup_card and not self.user_visible_blocker)

    @property
    def should_set_pending_internal_retool(self) -> bool:
        return bool(self.is_internal and not self.extras.get("dispatch_session"))

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
    terminal_success = bool(ok and not needs_followup and status != "needs_planner_resolution")
    if terminal_success:
        extras = _scrub_internal_success_extras(extras)
        metadata = {**metadata, "extras": extras}

    is_internal = is_internal_dispatch_continuation(
        metadata,
        ok=ok,
        needs_followup=needs_followup,
        status=status,
    )
    user_visible_blocker = is_user_visible_dispatch_blocker(metadata)
    suppress_user_followup_card = (
        False if terminal_success else bool(extras.get("suppress_user_followup_card"))
    )
    suppress_main_summary = is_internal or (
        suppress_user_followup_card and not user_visible_blocker
    )
    has_mismatch_data = bool(
        extras.get("mismatch_kind")
        or extras.get("mismatch_question")
    )
    is_mismatch = mismatch_card_should_show(
        is_internal=is_internal,
        suppressed=suppress_user_followup_card and not user_visible_blocker,
        has_mismatch_data=has_mismatch_data,
    )
    if is_mismatch:
        suppress_main_summary = True

    return WorkerFinishOutcome(
        metadata=metadata,
        extras=extras,
        terminal_success=terminal_success,
        is_internal=is_internal,
        user_visible_blocker=user_visible_blocker,
        suppress_user_followup_card=suppress_user_followup_card,
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
        "internal_campaign_continuation",
        "suppress_user_followup_card",
        "planner_resolution_needed",
        "mismatch_kind",
        "mismatch_question",
        "failure_constraint",
        "dispatch_spec_rejected",
    ):
        result.pop(key, None)
    return result
