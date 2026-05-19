"""Deprecated WorkerLogCard — kept for backward compatibility.

Replaced by InfoHubPane for the two-pane workspace.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QVBoxLayout, QFrame, QLabel, QPlainTextEdit

from aura.gui.theme import ACCENT, BORDER


class WorkerLogCard(QFrame):
    """[DEPRECATED] Card for typewriter worker activity log.

    Replaced by InfoHubPane for the two-pane workspace.  Kept for
    backward compatibility with any external importers.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workerLogCard")
        self.setStyleSheet(f"QFrame#workerLogCard {{ background: rgba(28, 28, 34, 0.4); border: 1px solid {BORDER}; border-radius: 8px; }}")
        layout = QVBoxLayout(self)
        self._header = QLabel("\u26a1 Worker Activity", self)
        self._header.setStyleSheet(f"color: {ACCENT}; font-weight: 700;")
        layout.addWidget(self._header)
        self._content_view = QPlainTextEdit(self)
        self._content_view.setReadOnly(True)
        self._content_view.setFont(QFont("Geist Mono", 10))
        self._content_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._content_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_view.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._content_view)
        self._full, self._visible, self._timer = "", "", QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.setInterval(20)

    def append_text(self, text: str, is_reasoning=False):
        self._full += text
        if not self._timer.isActive():
            self._timer.start()

    def _on_tick(self):
        if len(self._visible) >= len(self._full):
            self._timer.stop()
            return
        self._visible += self._full[len(self._visible):len(self._visible) + 2]
        self._content_view.setPlainText(self._visible)
        h = self._content_view.document().size().height() + 15
        self._content_view.setFixedHeight(int(max(120, min(h, 600))))
        # Auto-scroll to bottom
        sb = self._content_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self):
        self._timer.stop()
        self._full = ""
        self._visible = ""
        self._content_view.setPlainText("")
