"""External auxiliary window that keeps ``EdgeTabRail`` attached to the
outside of the main Aura window's right edge, without consuming any of the
main window's own layout space."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from aura.gui.edge_rails import EdgeTabRail

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow

_TRACKED_MAIN_WINDOW_EVENTS = frozenset(
    {
        QEvent.Type.Move,
        QEvent.Type.Resize,
        QEvent.Type.Show,
        QEvent.Type.Hide,
        QEvent.Type.WindowStateChange,
    }
)


class ExternalEdgeRailHost(QWidget):
    """Frameless tool window that hosts the existing ``EdgeTabRail`` and
    keeps it visually attached just outside the main Aura window's right
    edge — like a book/index tab protruding from the side of the app.

    This is the single owner of the rail's top-level geometry. It tracks
    the main window's move/resize/show/hide/minimize/maximize through one
    event filter and repositions itself accordingly. ``EdgeTabRail`` itself
    keeps owning its internal tab layout/content; controllers only change
    rail state/content and never touch geometry.
    """

    _BOTTOM_MARGIN = 28

    def __init__(self, main_window: "MainWindow") -> None:
        super().__init__(
            main_window,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self._main_window = main_window
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        self.rail = EdgeTabRail(self)
        layout.addWidget(self.rail)

        main_window.installEventFilter(self)

    # ------------------------------------------------------------------
    # Main-window event tracking (single owner of external geometry)
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        if obj is self._main_window and event.type() in _TRACKED_MAIN_WINDOW_EVENTS:
            self._reposition()
        return False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition()

    def closeEvent(self, event: QCloseEvent) -> None:
        # Owned by MainWindow — never actually close, just hide.
        event.ignore()
        self.hide()

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _reposition(self) -> None:
        main = self._main_window
        if main is None or not main.isVisible() or main.isMinimized():
            self.hide()
            return

        self.adjustSize()
        frame = main.frameGeometry()
        screen = main.screen()
        available = screen.availableGeometry() if screen is not None else None

        x = frame.right() + 1
        if available is not None and x + self.width() > available.right():
            # No usable desktop space beyond the right edge (e.g. maximized) —
            # dock against the window's own outer edge instead of going
            # off-screen or widening Aura's workspace.
            x = frame.right() - self.width()

        status_bar = main.statusBar()
        margin_bottom = (status_bar.height() if status_bar is not None else 0) + self._BOTTOM_MARGIN
        y = max(frame.top(), frame.bottom() - self.height() - margin_bottom)

        self.move(x, y)
        if not self.isVisible():
            self.show()
        self.raise_()
