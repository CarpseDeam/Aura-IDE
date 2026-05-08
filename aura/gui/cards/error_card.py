"""Red error card with optional retry button."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from aura.gui.theme import BG_ALT, BORDER, DANGER, FG


class ErrorCard(QFrame):
    retry_clicked = Signal()

    def __init__(self, title: str, message: str, show_retry: bool = False) -> None:
        super().__init__()
        self.setObjectName("errorCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        head = QLabel(title)
        head.setStyleSheet(f"color: {DANGER}; font-weight: 600;")
        layout.addWidget(head)
        body = QLabel(message)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet(f"color: {FG};")
        layout.addWidget(body)

        if show_retry:
            btn_layout = QHBoxLayout()
            btn_layout.setContentsMargins(0, 4, 0, 0)
            self._retry_btn = QPushButton("Retry")
            self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._retry_btn.setStyleSheet(
                f"color: {FG}; background: {BG_ALT}; border: 1px solid {BORDER}; padding: 4px 12px; border-radius: 4px;"
            )
            self._retry_btn.clicked.connect(self._on_retry)
            btn_layout.addWidget(self._retry_btn)
            btn_layout.addStretch(1)
            layout.addLayout(btn_layout)

    def _on_retry(self) -> None:
        self._retry_btn.setEnabled(False)
        self._retry_btn.setText("Retrying...")
        self.retry_clicked.emit()
