"""Inline card representing one tool call."""
from __future__ import annotations

import re

from PySide6.QtWidgets import QFrame, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget

from aura.gui.cards._helpers import _mono_font
from aura.gui.theme import BG, BORDER, DANGER, FG, FG_DIM, SUCCESS_DIM, WARN


class ToolCallCard(QFrame):
    """Inline card representing one tool call.

    Header: 📄 name(args)   [running|done|failed]
    Body (collapsed by default): args and result
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName("toolCard")
        self._name = name
        self._args_text = ""
        self._state = self.STATE_RUNNING
        self._result_text = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        self._header = QToolButton()
        self._header.setObjectName("sectionToggle")
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {FG_DIM}; }} "
            f"QToolButton#sectionToggle:hover {{ color: {FG}; }}"
        )
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)

        self._args_view = QPlainTextEdit()
        self._args_view.setReadOnly(True)
        self._args_view.setFont(_mono_font(9))
        self._args_view.setStyleSheet(
            f"background: {BG}; color: {FG_DIM}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        self._args_view.setFixedHeight(80)
        body_layout.addWidget(self._args_view)

        self._result_view = QPlainTextEdit()
        self._result_view.setReadOnly(True)
        self._result_view.setFont(_mono_font(9))
        self._result_view.setStyleSheet(
            f"background: {BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        self._result_view.setFixedHeight(100)
        self._result_view.setVisible(False)
        body_layout.addWidget(self._result_view)

        self._body.setVisible(False)
        layout.addWidget(self._body)

        self._refresh_header()

    def _toggle_body(self) -> None:
        self._body.setVisible(not self._body.isVisible())
        self._refresh_header()

    def _refresh_header(self) -> None:
        chev = "v" if self._body.isVisible() else ">"
        state_str = {
            self.STATE_RUNNING: "(running)",
            self.STATE_DONE: "(done)",
            self.STATE_FAILED: "(failed)",
        }[self._state]
        color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS_DIM,
            self.STATE_FAILED: DANGER,
        }[self._state]
        # Prefer a short args summary in the header for readability.
        summary = self._summarize_args()
        text = f"{chev} {self._name}({summary})  "
        self._header.setText(text)
        # Style the state suffix via a separate stylesheet snippet on the QToolButton:
        self._header.setText(text + state_str)
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {color}; }}"
        )

    def _summarize_args(self) -> str:
        if not self._args_text:
            return ""
        # Best-effort extraction without full JSON parse if it's already a clean string
        if '"path"' in self._args_text:
            m = re.search(r'"path"\s*:\s*"([^"]+)"', self._args_text)
            if m:
                return f'"{m.group(1)}"'
        if '"pattern"' in self._args_text:
            m = re.search(r'"pattern"\s*:\s*"([^"]+)"', self._args_text)
            if m:
                return f'"{m.group(1)}"'
        return self._args_text[:60].replace("\n", " ")

    def update_args(self, text: str) -> None:
        self._args_text = text
        self._args_view.setPlainText(text)
        self._auto_size_view(self._args_view, 80, 400)
        self._refresh_header()

    def set_result(self, ok: bool, result_text: str) -> None:
        self._state = self.STATE_DONE if ok else self.STATE_FAILED
        self._result_view.setPlainText(result_text)
        self._result_view.setVisible(True)
        self._auto_size_view(self._result_view, 100, 500)
        if not ok:
            self._body.setVisible(True)  # auto-expand failed
        self._refresh_header()

    def _auto_size_view(self, view: QPlainTextEdit, min_h: int, max_h: int) -> None:
        doc = view.document()
        doc_height = doc.size().height() + 12
        clamped = max(min_h, min(doc_height, max_h))
        view.setFixedHeight(int(clamped))
