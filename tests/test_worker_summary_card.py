from aura.gui.cards.worker_summary_card import WorkerSummaryCard
from aura.gui.theme import DANGER, SUCCESS, WARN
from aura.conversation.dispatch import WorkerOutcomeStatus


def test_validation_failure_header_is_not_harness_error() -> None:
    label, _ = WorkerSummaryCard._status_label(
        False,
        True,
        "Validation failed \u2014 Validation command failed (exit code 1): python -m py_compile a.py",
    )

    assert label == "Validation failed"


def test_internal_failure_header_is_harness_error() -> None:
    label, _ = WorkerSummaryCard._status_label(
        False,
        False,
        "Harness error \u2014 Harness error due to an internal Worker exception.",
    )

    assert label == "Harness error"


def test_worker_followup_header_is_not_harness_error() -> None:
    label, _ = WorkerSummaryCard._status_label(
        False,
        True,
        "Worker needs follow-up \u2014 blocked by missing dependency (worker_blocked).",
    )

    assert label == "Worker needs follow-up"


def test_status_driven_labels() -> None:
    """Test each WorkerOutcomeStatus maps to the expected label and color."""
    # Colors match aura.gui.theme constants used in the mapping
    tests = [
        (WorkerOutcomeStatus.completed.value, "Completed", SUCCESS),
        (WorkerOutcomeStatus.completed_with_caveats.value, "Completed with caveats", WARN),
        (WorkerOutcomeStatus.validation_failed.value, "Validation failed", DANGER),
        (WorkerOutcomeStatus.harness_error.value, "Harness error", DANGER),
        (WorkerOutcomeStatus.cancelled.value, "Cancelled", "#6b7280"),
        (WorkerOutcomeStatus.needs_followup.value, "Needs follow-up", WARN),
        (WorkerOutcomeStatus.edit_mechanics_blocked.value, "Edit mechanics blocked", WARN),
        (WorkerOutcomeStatus.craft_blocked.value, "Craft blocked", DANGER),
        (WorkerOutcomeStatus.craft_rejected.value, "Craft rejected", DANGER),
        (WorkerOutcomeStatus.scope_mismatch.value, "Scope mismatch", WARN),
        (WorkerOutcomeStatus.approval_rejected.value, "Approval rejected", DANGER),
    ]
    for status_val, expected_label, expected_color in tests:
        label, color = WorkerSummaryCard._status_label(ok=True, needs_followup=False, status=status_val)
        assert label == expected_label, f"{status_val}: expected {expected_label!r}, got {label!r}"
        assert color == expected_color, f"{status_val}: expected {expected_color!r}, got {color!r}"


def test_validation_failure_via_status() -> None:
    """Status-driven validation_failed should not fall back to harness error."""
    label, _ = WorkerSummaryCard._status_label(
        ok=False,
        needs_followup=False,
        status=WorkerOutcomeStatus.validation_failed.value,
    )
    assert label == "Validation failed", f"Expected Validation failed, got {label}"


def test_legacy_fallback() -> None:
    """Without status, should use legacy inference from ok/needs_followup/summary."""
    label, _ = WorkerSummaryCard._status_label(ok=True, needs_followup=False)
    assert label == "Completed"
    # Without status and without summary text, ok=False falls through to
    # "Worker needs follow-up" (legacy code cannot detect harness error
    # without the "Harness error" prefix in the summary text)
    label, _ = WorkerSummaryCard._status_label(ok=False, needs_followup=False)
    assert label == "Worker needs follow-up"
    # With summary starting with "Harness error", legacy code detects it
    label, _ = WorkerSummaryCard._status_label(
        ok=False,
        needs_followup=False,
        summary="Harness error \u2014 something failed",
    )
    assert label == "Harness error"
