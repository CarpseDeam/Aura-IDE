"""Tests for WorkerSummaryCard and parse_worker_summary_receipt."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel

from aura.gui.cards.worker_summary_card import (
    WorkerSummaryCard,
    parse_worker_summary_receipt,
)
from aura.gui.theme import DANGER, SUCCESS, WARN
from aura.conversation.dispatch import WorkerOutcomeStatus


# ── Parser tests (pure, no qapp needed) ────────────────────────────────────

FULL_RECEIPT = """\
══════════════════════════════════════
 ✅  Worker completed successfully
──────────────────────────────────────
 Files changed   : 3 (2 edited, 1 new)
 Validation      : ✓ py_compile (1/1 passed)
 Action needed   : None — ready for review
──────────────────────────────────────

 Modified files:
  • src/a.py   (edit)
  • src/b.py   (edit)
  • src/c.py   (new)

 Validation:
  • python -m py_compile src/a.py  →  passed

 Caveats:
  • Some caveat
  • Broad/multi-file task did not use update_todo_list — consider a visible plan next time.

 Summary:
  Fixed the bug by updating the validation logic.

══════════════════════════════════════
"""


def test_parse_extracts_summary_text() -> None:
    """Full receipt with a Summary section returns the expected line."""
    parsed = parse_worker_summary_receipt(FULL_RECEIPT)
    assert parsed["summary_text"] == "Fixed the bug by updating the validation logic."


def test_parse_extracts_file_counts() -> None:
    """'Files changed   : 3 (2 edited, 1 new)' → file_counts = {total:3, edited:2, new:1, deleted:0}."""
    parsed = parse_worker_summary_receipt(FULL_RECEIPT)
    assert parsed["file_counts"] == {"total": 3, "edited": 2, "new": 1, "deleted": 0}


def test_parse_extracts_validation() -> None:
    """'Validation      : ✓ py_compile (1/1 passed)' → validation string returned."""
    parsed = parse_worker_summary_receipt(FULL_RECEIPT)
    assert "py_compile" in parsed["validation"]
    assert "1/1 passed" in parsed["validation"]


def test_parse_removes_box_borders() -> None:
    """Detects has_box_borders=True."""
    parsed = parse_worker_summary_receipt(FULL_RECEIPT)
    assert parsed["has_box_borders"] is True


def test_parse_filters_todo_caveat() -> None:
    """TODO nag filtered from caveats list."""
    parsed = parse_worker_summary_receipt(FULL_RECEIPT)
    assert "Some caveat" in parsed["caveats"]
    assert not any(
        "update_todo_list" in c for c in parsed["caveats"]
    )


def test_parse_empty_input() -> None:
    """Empty string returns default dict."""
    parsed = parse_worker_summary_receipt("")
    assert parsed["summary_text"] == ""
    assert parsed["file_counts"] == {"total": 0, "edited": 0, "new": 0, "deleted": 0}
    assert parsed["caveats"] == []
    assert parsed["has_box_borders"] is False


def test_parse_no_summary_section() -> None:
    """Receipt without Summary section returns empty summary_text."""
    receipt = """\
══════════════════════════════════════
 ✅  Worker completed successfully
──────────────────────────────────────
 Files changed   : 1 (1 edited)
 Validation      : ✓ py_compile (1/1 passed)
 Action needed   : None — ready for review
──────────────────────────────────────

 Modified files:
  • src/a.py   (edit)

══════════════════════════════════════
"""
    parsed = parse_worker_summary_receipt(receipt)
    assert parsed["summary_text"] == ""
    assert parsed["file_counts"] == {"total": 1, "edited": 1, "new": 0, "deleted": 0}


def test_parse_file_counts_variants() -> None:
    """Test various file count formats."""
    # No counts (0)
    parsed = parse_worker_summary_receipt("Files changed   : 0")
    assert parsed["file_counts"] == {"total": 0, "edited": 0, "new": 0, "deleted": 0}

    # Only total
    parsed = parse_worker_summary_receipt("Files changed   : 5")
    assert parsed["file_counts"] == {"total": 5, "edited": 0, "new": 0, "deleted": 0}

    # Edited only
    parsed = parse_worker_summary_receipt("Files changed   : 2 (2 edited)")
    assert parsed["file_counts"] == {"total": 2, "edited": 2, "new": 0, "deleted": 0}

    # All three
    parsed = parse_worker_summary_receipt("Files changed   : 6 (3 edited, 2 new, 1 deleted)")
    assert parsed["file_counts"] == {"total": 6, "edited": 3, "new": 2, "deleted": 1}


# ── Status label tests ─────────────────────────────────────────────────────


def test_validation_failure_header_is_not_harness_error() -> None:
    label, _ = WorkerSummaryCard._status_label(
        False,
        True,
        "Validation failed \u2014 Validation command failed (exit code 1): python -m py_compile a.py",
    )

    assert label == "❌ Failed validation"


def test_internal_failure_header_is_harness_error() -> None:
    label, _ = WorkerSummaryCard._status_label(
        False,
        False,
        "Harness error \u2014 Harness error due to an internal Worker exception.",
    )

    assert label == "❌ Harness error"


def test_worker_followup_header_is_not_harness_error() -> None:
    label, _ = WorkerSummaryCard._status_label(
        False,
        True,
        "Worker needs follow-up \u2014 blocked by missing dependency (worker_blocked).",
    )

    assert label == "⚠️ Worker needs follow-up"


def test_status_driven_labels() -> None:
    """Test each WorkerOutcomeStatus maps to the expected label and color."""
    tests = [
        (WorkerOutcomeStatus.completed.value, "✅ Done", SUCCESS),
        (WorkerOutcomeStatus.completed_with_caveats.value, "✅ Done with caveats", WARN),
        (WorkerOutcomeStatus.validation_failed.value, "❌ Failed validation", DANGER),
        (WorkerOutcomeStatus.harness_error.value, "❌ Harness error", DANGER),
        (WorkerOutcomeStatus.cancelled.value, "🔶 Cancelled", "#6b7280"),
        (WorkerOutcomeStatus.needs_followup.value, "⚠️ Needs follow-up", WARN),
        (WorkerOutcomeStatus.edit_mechanics_blocked.value, "⚠️ Edit mechanics blocked", WARN),
        (WorkerOutcomeStatus.craft_blocked.value, "❌ Craft blocked", DANGER),
        (WorkerOutcomeStatus.craft_rejected.value, "❌ Craft rejected", DANGER),
        (WorkerOutcomeStatus.scope_mismatch.value, "⚠️ Scope mismatch", WARN),
        (WorkerOutcomeStatus.approval_rejected.value, "❌ Approval rejected", DANGER),
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
    assert label == "❌ Failed validation", f"Expected ❌ Failed validation, got {label}"


def test_legacy_fallback() -> None:
    """Without status, should use legacy inference from ok/needs_followup/summary."""
    label, _ = WorkerSummaryCard._status_label(ok=True, needs_followup=False)
    assert label == "✅ Done"
    # Without status and without summary text, ok=False falls through to
    # "⚠️ Worker needs follow-up" (legacy code cannot detect harness error
    # without the "Harness error" prefix in the summary text)
    label, _ = WorkerSummaryCard._status_label(ok=False, needs_followup=False)
    assert label == "⚠️ Worker needs follow-up"
    # With summary starting with "Harness error", legacy code detects it
    label, _ = WorkerSummaryCard._status_label(
        ok=False,
        needs_followup=False,
        summary="Harness error \u2014 something failed",
    )
    assert label == "❌ Harness error"


# ── Card rendering tests (need qapp) ───────────────────────────────────────


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_card_shows_compact_summary(qapp) -> None:
    """Full receipt input renders summary_text and footer, not section headers."""
    card = WorkerSummaryCard(
        tool_call_id="t1",
        goal="Fix the bug",
        ok=True,
        summary=FULL_RECEIPT,
    )
    labels = [w.text() for w in card.findChildren(QLabel)]
    # Summary text should appear
    assert any("Fixed the bug by updating" in t for t in labels)
    # Section headers should NOT appear as body text
    assert not any("Modified files:" in t for t in labels)
    assert not any("Validation:" in t for t in labels)
    # Footer should appear
    assert any("Details are in Worker Log." in t for t in labels)


def test_card_shows_stats_chips(qapp) -> None:
    """Receipt with file counts shows chip labels."""
    card = WorkerSummaryCard(
        tool_call_id="t1",
        goal="Fix the bug",
        ok=True,
        summary=FULL_RECEIPT,
    )
    labels = [w.text() for w in card.findChildren(QLabel)]
    assert any("3 files" in t for t in labels)
    assert any("2 edited" in t for t in labels)
    assert any("1 new" in t for t in labels)


def test_card_fallback_on_no_summary(qapp) -> None:
    """Receipt without Summary shows fallback, not raw borders."""
    receipt_no_summary = """\
══════════════════════════════════════
 ✅  Worker completed successfully
──────────────────────────────────────
 Files changed   : 1 (1 edited)
 Validation      : ✓ py_compile (1/1 passed)
 Action needed   : None — ready for review
──────────────────────────────────────

 Modified files:
  • src/a.py   (edit)

══════════════════════════════════════
"""
    card = WorkerSummaryCard(
        tool_call_id="t1",
        goal="Fix the bug",
        ok=True,
        summary=receipt_no_summary,
    )
    labels = [w.text() for w in card.findChildren(QLabel)]
    # Should NOT show raw border characters
    assert not any("══════════════════════════════════════" in t for t in labels)
    # Should show footer
    assert any("Details are in Worker Log." in t for t in labels)


def test_card_dedupe_still_works(qapp) -> None:
    """update_summary on existing tool_call_id updates card in place."""
    card = WorkerSummaryCard(
        tool_call_id="t1",
        goal="First goal",
        ok=False,
        summary="First summary",
    )
    first_labels = [w.text() for w in card.findChildren(QLabel)]

    card.update_summary(
        goal="Second goal",
        ok=True,
        summary=FULL_RECEIPT,
    )
    second_labels = [w.text() for w in card.findChildren(QLabel)]

    # Should have updated content
    assert any("Second goal" in t for t in second_labels)
    assert any("Fixed the bug" in t for t in second_labels)
    # Should not have old content
    assert not any("First goal" in t for t in second_labels)
    assert not any("First summary" in t for t in second_labels)


def test_worker_summary_card_inserted_by_default(qapp):
    """WorkerSummaryCard is inserted by add_worker_summary when not disabled."""
    from aura.gui.chat_view import ChatView
    view = ChatView()
    assert view.worker_summary_disabled is False
    view.add_worker_summary("tc1", "Test goal", True, "Receipt")
    assert "tc1" in view._worker_summary_cards
    card = view._worker_summary_cards["tc1"]
    labels = [w.text() for w in card.findChildren(QLabel)]
    assert "Test goal" in labels
