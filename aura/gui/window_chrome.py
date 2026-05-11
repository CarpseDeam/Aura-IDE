"""Mixin that provides frameless window chrome (gradient background, drag-to-move,
maximize/restore toggle) for a QMainWindow subclass.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QToolButton

from aura.gui.aura_widget import GlassSwitch


class WindowChromeMixin:
    """Mixin for a QMainWindow subclass that renders a dark radial gradient
    background and supports drag-to-move on a custom toolbar.

    The mixin expects the host class (or one of its other bases) to provide
    the full QMainWindow interface (isMaximized, showNormal, showMaximized,
    move, pos, rect, height, width, etc.).

    If the host has a ``_toolbar`` attribute (an instance of MainWindowToolbar
    or similar) the mixin will:
    - Allow dragging by left-clicking on non-interactive toolbar regions.
    - Update the toolbar's maximize icon when the window state changes.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]
        self._dragging = False
        self._drag_start_pos = None

    # ----- paintEvent: radial gradient background --------------------------

    def paintEvent(self, event) -> None:
        """Render a dark radial gradient background over the entire widget."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        center.setY(int(self.height() * 0.15))
        radius = max(self.width(), self.height()) * 0.8
        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0.0, QColor(30, 34, 46, 255))
        gradient.setColorAt(0.4, QColor(18, 20, 26, 255))
        gradient.setColorAt(1.0, QColor(6, 8, 12, 255))
        painter.fillRect(self.rect(), gradient)
        painter.end()
        super().paintEvent(event)  # type: ignore[misc]

    # ----- drag-to-move on toolbar -----------------------------------------

    def mousePressEvent(self, event) -> None:
        """Start a window drag when the left button is pressed on the toolbar,
        unless the click landed on an interactive widget (QToolButton, GlassSwitch).
        """
        if event.button() == Qt.MouseButton.LeftButton:
            toolbar = getattr(self, "_toolbar", None)
            if toolbar is not None:
                tb_geo = toolbar.geometry()
                if tb_geo.contains(event.position().toPoint()):
                    pos = toolbar.mapFrom(self, event.position().toPoint())
                    child = toolbar.childAt(pos)
                    if child is not None:
                        curr = child
                        is_interactive = False
                        while curr and curr != toolbar:
                            if isinstance(curr, (QToolButton, GlassSwitch)):
                                is_interactive = True
                                break
                            curr = curr.parent()

                        if is_interactive:
                            super().mousePressEvent(event)  # type: ignore[misc]
                            return

                    self._drag_start_pos = event.globalPosition().toPoint()
                    self._dragging = True
                    event.accept()
                    return
        super().mousePressEvent(event)  # type: ignore[misc]

    def mouseMoveEvent(self, event) -> None:
        """Move the window by the mouse delta while dragging."""
        if getattr(self, "_dragging", False):
            delta = event.globalPosition().toPoint() - self._drag_start_pos
            self.move(self.pos() + delta)
            self._drag_start_pos = event.globalPosition().toPoint()
            event.accept()
            return
        super().mouseMoveEvent(event)  # type: ignore[misc]

    def mouseReleaseEvent(self, event) -> None:
        """End the window drag on mouse release."""
        if getattr(self, "_dragging", False):
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)  # type: ignore[misc]

    # ----- window state helpers -------------------------------------------

    def _toggle_maximize(self) -> None:
        """Toggle the window between maximized and normal state."""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        toolbar = getattr(self, "_toolbar", None)
        if toolbar is not None:
            toolbar.update_maximize_icon(self.isMaximized())

    def changeEvent(self, event) -> None:
        """Update the maximize icon when the window state changes."""
        if event.type() == event.Type.WindowStateChange:
            toolbar = getattr(self, "_toolbar", None)
            if toolbar is not None:
                toolbar.update_maximize_icon(self.isMaximized())
        super().changeEvent(event)  # type: ignore[misc]
