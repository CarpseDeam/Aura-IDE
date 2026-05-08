"""User message card with optional image thumbnails."""
from __future__ import annotations

import base64

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from aura.gui.cards._helpers import _wrap_body_text
from aura.gui.theme import BORDER, DANGER, FG_BODY_USER


class UserCard(QFrame):
    def __init__(self, text: str, image_b64s: list[str] | None = None) -> None:
        super().__init__()
        self.setObjectName("userCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        header = QLabel("You")
        header.setObjectName("userHeader")
        layout.addWidget(header)

        if image_b64s:
            row = QHBoxLayout()
            row.setSpacing(8)
            for b64 in image_b64s:
                thumb = self._make_thumb(b64)
                row.addWidget(thumb)
            row.addStretch(1)
            layout.addLayout(row)

        if text:
            body = QLabel(text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setText(_wrap_body_text(text, FG_BODY_USER))
            layout.addWidget(body)

    def _make_thumb(self, b64: str) -> QLabel:
        try:
            data = base64.b64decode(b64)
            pix = QPixmap()
            pix.loadFromData(data)
            scaled = pix.scaled(
                160, 120,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label = QLabel()
            label.setPixmap(scaled)
            label.setStyleSheet(f"border: 1px solid {BORDER}; border-radius: 4px;")
            return label
        except Exception as exc:
            label = QLabel(f"[image: {exc}]")
            label.setStyleSheet(f"color: {DANGER};")
            return label
