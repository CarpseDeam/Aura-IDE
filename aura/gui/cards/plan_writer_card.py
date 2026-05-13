"""Card for showing a worker plan being written in real time."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards._helpers import _fade_in_widget, _mono_font
from aura.gui.theme import BG, BORDER, DANGER, FG, FG_DIM, SUCCESS, WARN


class PlanWriterCard(QFrame):
    """Card for showing a worker plan being written in real time.

    Header: "📝 Planning…" with collapsible toggle.
    Body: goal label + spec view that streams character-by-character.
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toolCard")
        self.setMinimumWidth(0)
        self._goal: str = ""
        self._state = self.STATE_RUNNING

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        # Header
        self._header = QToolButton(self)
        self._header.setObjectName("sectionToggle")
        self._header.setMinimumWidth(0)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {FG_DIM}; }} "
            f"QToolButton#sectionToggle:hover {{ color: {FG}; }}"
        )
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        # Body
        self._body = QWidget(self)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)

        # Goal subtitle
        self._goal_label = QLabel("", self)
        self._goal_label.setMinimumWidth(0)
        self._goal_label.setWordWrap(True)
        self._goal_label.setStyleSheet(
            f"color: {FG_DIM}; font-style: italic; font-size: 11px; padding-bottom: 4px;"
        )
        self._goal_label.setVisible(False)
        body_layout.addWidget(self._goal_label)

        # Spec view
        self._spec_view = QPlainTextEdit(self)
        self._spec_view.setReadOnly(True)
        self._spec_view.setMinimumWidth(0)
        self._spec_view.setFont(_mono_font(10))
        self._spec_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._spec_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px; }}"
        )
        body_layout.addWidget(self._spec_view)

        self._body.setVisible(False)
        layout.addWidget(self._body)

        self._refresh_header()
        _fade_in_widget(self)

    def _toggle_body(self) -> None:
        self._body.setVisible(not self._body.isVisible())
        self._refresh_header()

    def _refresh_header(self) -> None:
        chev = "v" if self._body.isVisible() else ">"
        state_str = {
            self.STATE_RUNNING: "…",
            self.STATE_DONE: "Ready ✓",
            self.STATE_FAILED: "Failed ✗",
        }[self._state]
        state_color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS,
            self.STATE_FAILED: DANGER,
        }[self._state]
        
        label = self._goal if self._goal else "Writing plan…"
        prefix = f"{chev} ⚡ "
        suffix = f"  {state_str}"
        metrics = QFontMetrics(self._header.font())
        available = max(40, self._header.width() - 10)
        label_width = max(24, available - metrics.horizontalAdvance(prefix + suffix))
        label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, label_width)
        text = f"{prefix}{label}{suffix}"
        self._header.setText(text)
        self._header.setToolTip(self._goal or "Writing plan")
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {state_color}; }}"
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_header()

    def set_goal(self, goal: str) -> None:
        self._goal = goal
        self._goal_label.setText(goal)
        self._goal_label.setVisible(True)
        self._refresh_header()

    def update_spec(self, spec: str) -> None:
        """Update spec content and adjust height."""
        self._spec_view.setPlainText(spec)
        self._auto_size_view()
        if not self._body.isVisible():
            self._body.setVisible(True)

    def _auto_size_view(self) -> None:
        doc = self._spec_view.document()
        doc.setDocumentMargin(4)
        doc_height = doc.size().height() + 12
        # Start at 120 (approx 7-8 lines), max out at 600
        clamped = max(120, min(doc_height, 600))
        self._spec_view.setFixedHeight(int(clamped))

    def set_result(self, ok: bool) -> None:
        self._state = self.STATE_DONE if ok else self.STATE_FAILED
        if not ok:
            # Auto-expand body on failure
            self._body.setVisible(True)
        self._refresh_header()
