"""Tests for WindowChromeMixin — frameless window chrome behaviour.

All tests use plain Mock objects and data-only Qt types so they do NOT
require a running QApplication.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QPoint, QRect, QEvent

from aura.gui.window_chrome import WindowChromeMixin


# ---------------------------------------------------------------------------
# Helper base: provides no-op event handlers so that the mixin's super()
# calls succeed without needing a real QMainWindow/QWidget.
# ---------------------------------------------------------------------------

class MockWindowBase:
    """Stub base with no-op event handlers to satisfy MRO super() calls."""
    def paintEvent(self, event): pass
    def mousePressEvent(self, event): pass
    def mouseMoveEvent(self, event): pass
    def mouseReleaseEvent(self, event): pass
    def changeEvent(self, event): pass


class MockMainWindow(WindowChromeMixin, MockWindowBase):
    """A test-friendly class that uses the mixin but replaces all
    QMainWindow behaviours with Mocks.
    """
    def __init__(self):
        super().__init__()
        self.isMaximized = Mock(return_value=False)
        self.showNormal = Mock()
        self.showMaximized = Mock()
        self.move = Mock()
        self.pos = Mock(return_value=QPoint(0, 0))
        self.rect = Mock(return_value=QRect(0, 0, 800, 600))
        self.height = Mock(return_value=600)
        self.width = Mock(return_value=800)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def window():
    return MockMainWindow()


@pytest.fixture
def toolbar():
    """A toolbar mock with default 100x30 geometry."""
    tb = Mock()
    tb.geometry.return_value = QRect(0, 0, 100, 30)
    tb.mapFrom.return_value = QPoint(10, 10)
    tb.childAt.return_value = None
    return tb


def _left_click_event(global_pos: QPoint, local_pos: QPoint | None = None):
    """Build a mock mouse event representing a left-button press."""
    event = Mock()
    event.button.return_value = Qt.MouseButton.LeftButton
    event.globalPosition.return_value.toPoint.return_value = global_pos
    event.position.return_value.toPoint.return_value = (
        local_pos if local_pos is not None else global_pos
    )
    return event


# =========================================================================
# Tests – drag start
# =========================================================================


def test_drag_starts_on_toolbar_press(window, toolbar):
    """Dragging begins when left-clicking on a non-interactive toolbar area."""
    window._toolbar = toolbar
    # Click inside the toolbar (local pos (50,15) is within QRect(0,0,100,30))
    event = _left_click_event(QPoint(200, 200), local_pos=QPoint(50, 15))
    window.mousePressEvent(event)
    assert window._dragging is True
    assert window._drag_start_pos == QPoint(200, 200)


def test_drag_skipped_on_interactive_widget(window, toolbar):
    """Clicking a QToolButton on the toolbar does NOT start dragging."""
    from PySide6.QtWidgets import QToolButton

    window._toolbar = toolbar
    toolbar.childAt.return_value = Mock(spec=QToolButton)
    event = _left_click_event(QPoint(200, 200), local_pos=QPoint(50, 15))
    window.mousePressEvent(event)
    assert window._dragging is False


def test_drag_skipped_on_non_toolbar_click(window):
    """Left-clicking outside the toolbar geometry does NOT start dragging."""
    window._toolbar = Mock()
    window._toolbar.geometry.return_value = QRect(0, 0, 100, 30)
    # Click at a position well outside the toolbar
    event = _left_click_event(QPoint(500, 500), local_pos=QPoint(500, 500))
    window.mousePressEvent(event)
    assert window._dragging is False


# =========================================================================
# Tests – drag movement & release
# =========================================================================


def test_mouse_move_drags_window(window):
    """While dragging, mouse movement shifts the window position."""
    window._dragging = True
    window._drag_start_pos = QPoint(100, 100)
    window.move = Mock()

    event = Mock()
    event.globalPosition.return_value.toPoint.return_value = QPoint(150, 120)
    window.mouseMoveEvent(event)

    # The mixin computes: delta = event_pos - drag_start_pos = (50, 20)
    # then calls self.move(self.pos() + delta).
    # self.pos() returns QPoint(0, 0) (from the mock).
    delta = QPoint(150, 120) - QPoint(100, 100)  # = QPoint(50, 20)
    expected_pos = QPoint(0, 0) + delta  # pos() + delta
    window.move.assert_called_once_with(expected_pos)
    # drag_start_pos should be updated
    assert window._drag_start_pos == QPoint(150, 120)


def test_mouse_release_ends_drag(window):
    """Releasing the mouse button stops dragging."""
    window._dragging = True
    event = Mock()
    window.mouseReleaseEvent(event)
    assert window._dragging is False


# =========================================================================
# Tests – maximize / restore toggling
# =========================================================================


def test_toggle_maximize_normal_to_max(window):
    """_toggle_maximize maximizes when the window is normal."""
    window.isMaximized.return_value = False
    window._toolbar = Mock()

    window._toggle_maximize()

    window.showMaximized.assert_called_once()


def test_toggle_maximize_max_to_normal(window):
    """_toggle_maximize restores the window when it is maximized."""
    window.isMaximized.return_value = True
    window._toolbar = Mock()

    window._toggle_maximize()

    window.showNormal.assert_called_once()


# =========================================================================
# Tests – change event
# =========================================================================


def test_change_event_updates_icon(window):
    """changeEvent updates the maximize icon when window state changes."""
    window.isMaximized.return_value = True
    window._toolbar = Mock()

    event = Mock()
    event.type.return_value = QEvent.Type.WindowStateChange
    # Make event.Type resolve to the real enum so that
    # event.Type.WindowStateChange compares equal to event.type().
    event.Type = QEvent.Type

    window.changeEvent(event)

    window._toolbar.update_maximize_icon.assert_called_once_with(True)


def test_change_event_no_toolbar(window):
    """changeEvent does not crash when _toolbar is absent."""
    window.isMaximized.return_value = False
    # No toolbar set

    event = Mock()
    event.type.return_value = QEvent.Type.WindowStateChange
    event.Type = QEvent.Type

    # Should not raise
    window.changeEvent(event)


def test_change_event_non_state_change(window):
    """changeEvent ignores events that are not WindowStateChange."""
    window.isMaximized.return_value = True
    window._toolbar = Mock()

    event = Mock()
    event.type.return_value = QEvent.Type.Move  # not WindowStateChange
    event.Type = QEvent.Type

    window.changeEvent(event)

    window._toolbar.update_maximize_icon.assert_not_called()
