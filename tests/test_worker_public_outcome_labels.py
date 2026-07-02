"""Tests for _final_summary_label public outcome labels.

Verifies that every WorkerOutcomeStatus produces the correct label string
and that no scary/confusing words leak into the label.
"""

from __future__ import annotations

import pytest

from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.gui.info_hub_pane import _activity_log_line, _final_summary_label

EXPECTED_LABELS = {
    WorkerOutcomeStatus.completed: "Worker Report.",
    WorkerOutcomeStatus.completed_with_caveats: "Worker Report.",
    WorkerOutcomeStatus.validation_failed: "Worker Report.",
    WorkerOutcomeStatus.edit_mechanics_blocked: "Worker Report.",
    WorkerOutcomeStatus.scope_mismatch: "Worker Report.",
    WorkerOutcomeStatus.needs_followup: "Worker Report.",
    WorkerOutcomeStatus.approval_rejected: "Changes rejected.",
    WorkerOutcomeStatus.cancelled: "Cancelled.",
    WorkerOutcomeStatus.harness_error: "Worker Error.",
}

NON_HARNESS_STATUSES = [
    WorkerOutcomeStatus.completed,
    WorkerOutcomeStatus.completed_with_caveats,
    WorkerOutcomeStatus.validation_failed,
    WorkerOutcomeStatus.edit_mechanics_blocked,
    WorkerOutcomeStatus.scope_mismatch,
    WorkerOutcomeStatus.needs_followup,
    WorkerOutcomeStatus.approval_rejected,
    WorkerOutcomeStatus.cancelled,
    "no_progress",
    "edit_mechanism_failed",
    None,
]


class TestFinalSummaryLabelMapping:
    """Every WorkerOutcomeStatus maps to the expected label string."""

    @pytest.mark.parametrize("status,expected", list(EXPECTED_LABELS.items()))
    def test_label_mapping(self, status: str, expected: str) -> None:
        label = _final_summary_label(ok=True, status=status)
        assert label == expected, (
            f"Expected {expected!r} for status={status!r}, got {label!r}"
        )


class TestFinalSummaryNoScaryWords:
    """Labels must not contain alarming or confusing terminology."""

    FORBIDDEN = ["Failed", "Needs attention", "Completed"]

    @pytest.mark.parametrize("status", NON_HARNESS_STATUSES)
    def test_no_scary_words(self, status: str | None) -> None:
        label = _final_summary_label(ok=True, status=status)
        for word in self.FORBIDDEN:
            assert word not in label, (
                f"Label {label!r} contains forbidden word {word!r} "
                f"for status={status!r}"
            )


class TestWorkerActivityLogLine:
    """Activity projection keeps the Worker Log focused on tool work."""

    @pytest.mark.parametrize(
        "entry,expected",
        [
            (
                {"kind": "tool_started", "message": "Tool started: write_file"},
                "Tool started: write_file",
            ),
            (
                {"kind": "command_finished", "message": "Command exit 0"},
                "Command exit 0",
            ),
            (
                {"kind": "validation_passed", "message": "Validation passed: pytest"},
                "Validation passed: pytest",
            ),
        ],
    )
    def test_tool_and_command_activity_is_visible(
        self,
        entry: dict,
        expected: str,
    ) -> None:
        assert _activity_log_line(entry) == expected

    @pytest.mark.parametrize(
        "kind",
        [
            "campaign_started",
            "step_started",
            "step_completed",
            "final_report_started",
            "final_report_completed",
        ],
    )
    def test_checklist_and_final_report_activity_is_suppressed(
        self,
        kind: str,
    ) -> None:
        assert _activity_log_line({"kind": kind, "message": "noise"}) == ""
