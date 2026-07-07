"""Collapsible section widget — toggle button + body."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout, QWidget


class _CollapsibleSection(QFrame):
    """A toggle button + body widget that collapses on click."""

    OPEN_CARET = "\u25be"   # ▾
    CLOSED_CARET = "\u25b8"  # ▸

    def __init__(
        self,
        title: str,
        body: QWidget,
        start_open: bool = False,
        prominent: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._toggle = QToolButton(self)
        self._toggle.setObjectName("reasoningToggle" if prominent else "sectionToggle")
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(self._on_toggle)
        self._title = title
        self._body = body
        self._open = start_open
        layout.addWidget(self._toggle)
        layout.addWidget(body)
        body.setVisible(start_open)
        self._refresh_text()

    def _refresh_text(self) -> None:
        caret = self.OPEN_CARET if self._open else self.CLOSED_CARET
        self._toggle.setText(f"{caret}  {self._title}")

    def _on_toggle(self) -> None:
        self._open = not self._open
        self._body.setVisible(self._open)
        self._refresh_text()
        self._after_visibility_change()

    def _after_visibility_change(self) -> None:
        if self._open and hasattr(self._body, "refresh_height"):
            self._body.refresh_height()
        self._body.updateGeometry()
        self.updateGeometry()

    def set_title(self, title: str) -> None:
        self._title = title
        self._refresh_text()

    def set_open(self, value: bool) -> None:
        self._open = value
        self._body.setVisible(value)
        self._refresh_text()
        self._after_visibility_change()
