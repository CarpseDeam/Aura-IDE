"""Tests for SpecCard dispatch status labels.

Verifies that ``spec_finished_label`` and ``spec_replay_finished_label``
produce appropriate labels and that forbidden wording never appears in
visible Planner/Worker chat card labels.
"""

from __future__ import annotations

import pytest

from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.gui.cards.dispatch_status_labels import (
    spec_finished_label,
    spec_replay_finished_label,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

FORBIDDEN = ("Failed", "Needs attention")
"""Labels that MUST NOT appear in any visible (non-internal) chat card path."""


def _assert_no_forbidden(label: str, context: str) -> None:
    for word in FORBIDDEN:
        assert word not in label, (
            f"Forbidden word {word!r} found in label {label!r} ({context})"
        )


# ---------------------------------------------------------------------------
# spec_finished_label — non-internal
# ---------------------------------------------------------------------------

EXPECTED_NONINTERNAL_TEXT: dict[WorkerOutcomeStatus, str] = {
    WorkerOutcomeStatus.completed: "Completed",
    WorkerOutcomeStatus.completed_with_caveats: "Completed",
    WorkerOutcomeStatus.validation_failed: "Worker Report",
    WorkerOutcomeStatus.edit_mechanics_blocked: "Worker Report",
    WorkerOutcomeStatus.scope_mismatch: "Worker Report",
    WorkerOutcomeStatus.needs_followup: "Worker Report",
    WorkerOutcomeStatus.approval_rejected: "Changes rejected",
    WorkerOutcomeStatus.cancelled: "Cancelled",
    WorkerOutcomeStatus.harness_error: "Worker Error",
}
"""Expected label text per status for non-internal calls."""


class TestSpecFinishedLabelNonInternal:
    """spec_finished_label(is_internal=False) with explicit status."""

    @pytest.mark.parametrize(
        "status,expected_text",
        list(EXPECTED_NONINTERNAL_TEXT.items()),
    )
    def test_label_per_status(self, status: WorkerOutcomeStatus, expected_text: str) -> None:
        text, _ = spec_finished_label(ok=True, status=status.value)
        assert text == expected_text, (
            f"Expected text {expected_text!r} for {status}, got {text!r}"
        )
        _assert_no_forbidden(text, f"spec_finished_label({status})")

    def test_normalized_status_string(self) -> None:
        """An unrecognized status string falls through to the ok-based fallback."""
        text, _ = spec_finished_label(ok=True, status="some_unknown_status")
        assert text == "Completed"
        _assert_no_forbidden(text, "unknown status, ok=True")

    def test_ok_false_no_status(self) -> None:
        """Fallback when ok=False and status is None."""
        text, _ = spec_finished_label(ok=False)
        assert text == "Worker Report"
        _assert_no_forbidden(text, "ok=False, status=None")

    def test_ok_true_no_status(self) -> None:
        """Fallback when ok=True and status is None."""
        text, _ = spec_finished_label(ok=True)
        assert text == "Completed"
        _assert_no_forbidden(text, "ok=True, status=None")


class TestSpecFinishedLabelInternal:
    """spec_finished_label with is_internal=True — should be brief."""

    @pytest.mark.parametrize(
        "status,ok,expected_text",
        [
            (WorkerOutcomeStatus.completed, True, "Completed"),
            (WorkerOutcomeStatus.completed_with_caveats, True, "Completed"),
            (WorkerOutcomeStatus.validation_failed, True, "Completed"),
            (WorkerOutcomeStatus.edit_mechanics_blocked, True, "Completed"),
            (WorkerOutcomeStatus.harness_error, False, "Worker Report"),
            (None, True, "Completed"),
            (None, False, "Worker Report"),
        ],
    )
    def test_internal_labels(self, status: str | None, ok: bool, expected_text: str) -> None:
        text, _ = spec_finished_label(
            ok=ok,
            status=status.value if isinstance(status, WorkerOutcomeStatus) else status,
            is_internal=True,
        )
        assert text == expected_text, (
            f"Expected {expected_text!r} for internal status={status}, got {text!r}"
        )
        _assert_no_forbidden(text, f"internal status={status}, ok={ok}")


# ---------------------------------------------------------------------------
# spec_replay_finished_label
# ---------------------------------------------------------------------------


class TestSpecReplayFinishedLabel:
    """spec_replay_finished_label — history replay path (no status available)."""

    @pytest.mark.parametrize(
        "ok,expected_text",
        [
            (True, "Completed"),
            (False, "Worker Error"),
        ],
    )
    def test_noninternal(
        self, ok: bool, expected_text: str
    ) -> None:
        text, _ = spec_replay_finished_label(ok=ok, is_internal=False)
        assert text == expected_text
        _assert_no_forbidden(text, f"replay ok={ok}")

    @pytest.mark.parametrize(
        "ok,expected_text",
        [
            (True, "Completed"),
            (False, "Worker Report"),
        ],
    )
    def test_internal(self, ok: bool, expected_text: str) -> None:
        text, _ = spec_replay_finished_label(ok=ok, is_internal=True)
        assert text == expected_text, (
            f"Expected {expected_text!r} for internal replay ok={ok}, got {text!r}"
        )
        _assert_no_forbidden(text, f"internal replay ok={ok}")


# ---------------------------------------------------------------------------
# Sweep: no forbidden label in any non-internal code path
# ---------------------------------------------------------------------------


class TestNoForbiddenLabelsInChatPaths:
    """Every non-internal status combination must avoid forbidden wording.

    This sweep covers every ``WorkerOutcomeStatus`` value plus common
    edge values (None, raw strings) across ``spec_finished_label``.
    """

    STATUS_VALUES: list[str | None] = [
        s.value for s in WorkerOutcomeStatus
    ] + [
        None,
        "no_progress",
        "edit_mechanism_failed",
        "completed",  # raw value already in list, but explicit for clarity
    ]

    @pytest.mark.parametrize("status", STATUS_VALUES)
    def test_spec_finished_no_forbidden(self, status: str | None) -> None:
        """spec_finished_label(ok=True, status=...) must not produce forbidden words."""
        text, _ = spec_finished_label(ok=True, status=status)
        _assert_no_forbidden(text, f"spec_finished_label(status={status!r})")

    @pytest.mark.parametrize("status", STATUS_VALUES)
    def test_spec_finished_no_forbidden_ok_false(self, status: str | None) -> None:
        """Same for ok=False — no statuses should produce forbidden words."""
        text, _ = spec_finished_label(ok=False, status=status)
        _assert_no_forbidden(text, f"spec_finished_label(ok=False, status={status!r})")

    @pytest.mark.parametrize("ok", [True, False])
    def test_spec_replay_no_forbidden(self, ok: bool) -> None:
        """spec_replay_finished_label must not produce forbidden words."""
        text, _ = spec_replay_finished_label(ok=ok)
        _assert_no_forbidden(text, f"spec_replay_finished_label(ok={ok})")
