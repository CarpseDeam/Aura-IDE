"""Mixin that provides frameless window chrome (gradient background, drag-to-move,
maximize/restore toggle) for a QMainWindow subclass.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QToolButton

from aura.gui.widgets.glass_switch import GlassSwitch


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
        Edge/corner resize zones win over toolbar drag.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            # Edge/corner resize — wins over toolbar drag.
            if not self.isMaximized():
                edges = self._resolve_resize_edges(event.position().toPoint())
                if edges is not None:
                    self._start_system_resize(edges)
                    event.accept()
                    return

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

    # ----- native event: WM_NCHITTEST resize edges/corners -----------------

    def nativeEvent(self, eventType, message):
        """Handle WM_NCHITTEST on Windows to enable resize from all edges/corners."""
        # Normalize eventType — PySide6 may pass QByteArray or bytes.
        try:
            raw_type = bytes(eventType)
        except (TypeError, ValueError):
            raw_type = None

        if raw_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG") and not self.isMaximized():
            import ctypes
            from ctypes import wintypes

            # Extract MSG pointer — prefer __int__() over int() for ctypes pointers.
            try:
                addr = message.__int__()
            except (TypeError, AttributeError):
                try:
                    addr = int(message)
                except (TypeError, ValueError):
                    return super().nativeEvent(eventType, message)

            try:
                msg = wintypes.MSG.from_address(addr)
            except Exception:
                return super().nativeEvent(eventType, message)

            if msg.message == 0x0084:  # WM_NCHITTEST
                # Logical global cursor position (Qt-scaled), matches frameGeometry units.
                from PySide6.QtGui import QCursor
                cursor = QCursor.pos()
                x = cursor.x()
                y = cursor.y()
                rect = self.frameGeometry()
                margin = 8

                left = x <= rect.left() + margin
                right = x >= rect.right() - margin
                top = y <= rect.top() + margin
                bottom = y >= rect.bottom() - margin

                if top and left:
                    return True, 13  # HTTOPLEFT
                elif top and right:
                    return True, 14  # HTTOPRIGHT
                elif bottom and left:
                    return True, 16  # HTBOTTOMLEFT
                elif bottom and right:
                    return True, 17  # HTBOTTOMRIGHT
                elif left:
                    return True, 10  # HTLEFT
                elif right:
                    return True, 11  # HTRIGHT
                elif top:
                    return True, 12  # HTTOP
                elif bottom:
                    return True, 15  # HTBOTTOM

        return super().nativeEvent(eventType, message)

    def _resolve_resize_edges(self, pos):
        """Return Qt.Edges flags for the resize zone at *pos* (client coords), or None."""
        rect = self.rect()
        margin = 8
        edges = Qt.Edges()
        if pos.x() <= margin:
            edges |= Qt.Edge.LeftEdge
        if pos.x() >= rect.width() - margin:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= margin:
            edges |= Qt.Edge.TopEdge
        if pos.y() >= rect.height() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges if edges else None

    def _start_system_resize(self, edges):
        """Initiate an OS resize operation for the given edges."""
        window = self.windowHandle()
        if window is not None:
            window.startSystemResize(edges)
