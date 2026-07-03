"""WorkerTodoWidget row-stability and step-local replacement tests.

Validates that the TODO widget:
- Reuses stable rows at the same position without unnecessary re-insertion
- Creates new rows when step-local snapshot items change
- Removes stale rows when step transitions bring different TODO items
- Shows/hides properly on empty vs populated snapshots
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

import pytest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget


# ── QApplication singleton for the test session ──────────────────────────
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def host(qapp):
    """A plain QWidget to act as parent for the TODO widget."""
    return QWidget()


# ── Helpers ─────────────────────────────────────────────────────────────

def _snap(items=None):
    """Build a minimal valid TODO snapshot dict."""
    if items is None:
        items = [
            {"id": "inspect", "text": "Inspect current path", "status": "done"},
            {"id": "edit", "text": "Patch the implementation", "status": "active"},
            {"id": "verify", "text": "Run focused validation", "status": "pending"},
        ]
    return items


def _row_ids(widget):
    """Return the ordered list of row ids currently in the widget layout."""
    ids = []
    for i in range(widget._rows_layout.count()):
        item = widget._rows_layout.itemAt(i)
        if item is not None and item.widget():
            # Find the item_id for this widget
            for rid, row in widget._rows.items():
                if row.widget is item.widget():
                    ids.append(rid)
                    break
    return ids


# ── Tests ───────────────────────────────────────────────────────────────


class TestWorkerTodoWidgetRowStability:
    """Rows at stable positions stay where they are — no unnecessary re-insert."""

    def test_initial_snapshot_renders_all_rows(self, host):
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        items = _snap()
        widget.update_snapshot(items)

        # widget is not in a shown window, so isVisible() may be False;
        # verify the rows are there and the widget is not hidden.
        assert not widget.isHidden()
        assert len(widget._rows) == 3
        assert set(widget._rows.keys()) == {"inspect", "edit", "verify"}
        assert _row_ids(widget) == ["inspect", "edit", "verify"]

    def test_stable_rows_are_not_reinserted(self, host):
        """When the same items arrive in the same order, insertWidget is NOT called
        for rows already at their correct index."""
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        items = _snap()
        widget.update_snapshot(items)

        # Now spy on the layout's insertWidget
        layout = widget._rows_layout
        original_insert = layout.insertWidget
        insert_calls = []

        def spy_insert(index, w):
            insert_calls.append((index, w))
            return original_insert(index, w)

        layout.insertWidget = spy_insert

        try:
            widget.update_snapshot(items)
        finally:
            layout.insertWidget = original_insert

        # No insertWidget calls should have been made — all rows are at
        # their correct positions from the first render.
        assert insert_calls == [], (
            f"Expected 0 insertWidget calls for stable rows, got {len(insert_calls)}"
        )

    def test_rows_repositioned_when_order_changes(self, host):
        """When items change order, only the affected rows get repositioned."""
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        items = _snap()
        widget.update_snapshot(items)

        # Reversed order
        reversed_items = list(reversed(items))
        widget.update_snapshot(reversed_items)

        assert _row_ids(widget) == ["verify", "edit", "inspect"]

    def test_stale_rows_removed_on_step_transition(self, host):
        """When a new step sends a different TODO snapshot, stale rows are removed."""
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        step1_items = [
            {"id": "step1-a", "text": "Step 1 task A", "status": "done"},
            {"id": "step1-b", "text": "Step 1 task B", "status": "active"},
            {"id": "step1-c", "text": "Step 1 task C", "status": "pending"},
        ]
        widget.update_snapshot(step1_items)
        assert set(widget._rows.keys()) == {"step1-a", "step1-b", "step1-c"}

        step2_items = [
            {"id": "step2-x", "text": "Step 2 task X", "status": "pending"},
            {"id": "step2-y", "text": "Step 2 task Y", "status": "active"},
            {"id": "step2-z", "text": "Step 2 task Z", "status": "pending"},
        ]
        widget.update_snapshot(step2_items)

        # All old rows gone, new rows present
        assert set(widget._rows.keys()) == {"step2-x", "step2-y", "step2-z"}
        assert _row_ids(widget) == ["step2-x", "step2-y", "step2-z"]

    def test_empty_snapshot_hides_widget(self, host):
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        widget.update_snapshot(_snap())
        assert not widget.isHidden()

        widget.update_snapshot([])
        assert widget.isHidden()
        assert len(widget._rows) == 0

    def test_clear_removes_all_rows_and_hides(self, host):
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        widget.update_snapshot(_snap())
        assert not widget.isHidden()

        widget.clear()
        assert widget.isHidden()
        assert len(widget._rows) == 0

    def test_row_status_update_without_repositioning(self, host):
        """Updating just the status of an item at the same position should not
        trigger any insertWidget calls."""
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        items = _snap()
        widget.update_snapshot(items)

        layout = widget._rows_layout
        original_insert = layout.insertWidget
        insert_calls = []

        def spy_insert(index, w):
            insert_calls.append((index, w))
            return original_insert(index, w)

        layout.insertWidget = spy_insert

        try:
            # Same items, same order, just status changed
            updated = [
                {"id": "inspect", "text": "Inspect current path", "status": "done"},
                {"id": "edit", "text": "Patch the implementation", "status": "done"},
                {"id": "verify", "text": "Run focused validation", "status": "active"},
            ]
            widget.update_snapshot(updated)
        finally:
            layout.insertWidget = original_insert

        assert insert_calls == [], (
            f"Expected 0 insertWidget calls for status-only update, got {len(insert_calls)}"
        )
        # Text updated
        assert widget._rows["edit"].status == "done"
        assert widget._rows["verify"].status == "active"

    def test_show_only_called_when_not_visible(self, host):
        """show() should not be called when the widget is already shown."""
        from aura.gui.widgets.worker_todo import WorkerTodoWidget

        widget = WorkerTodoWidget(host)
        widget.hide()
        assert widget.isHidden()

        # First snapshot — should call show()
        items = _snap()
        with patch.object(widget, "show", wraps=widget.show) as spy_show:
            widget.update_snapshot(items)
            spy_show.assert_called_once()
        assert not widget.isHidden()

        # Second snapshot — widget already shown, should NOT call show()
        with patch.object(widget, "show", wraps=widget.show) as spy_show:
            widget.update_snapshot(items)
            spy_show.assert_not_called()
