"""User message card with optional image thumbnails."""
from __future__ import annotations

import base64

from PySide6.QtCore import Qt, QBuffer, QByteArray
from PySide6.QtGui import QPixmap, QMovie
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import BORDER, DANGER, FG_BODY_USER


class UserCard(QFrame):
    def __init__(self, text: str, image_b64s: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("userCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        header = QLabel("You")
        header.setObjectName("userHeader")
        layout.addWidget(header)

        self._movies: list[QMovie] = []

        if image_b64s:
            row = QHBoxLayout()
            row.setSpacing(8)
            for b64 in image_b64s:
                thumb = self._make_thumb(b64)
                row.addWidget(thumb)
            row.addStretch(1)
            layout.addLayout(row)

        if text:
            body = QLabel()
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setText(_render_markdown_with_code(text))
            body.setStyleSheet(f"color: {FG_BODY_USER};")
            layout.addWidget(body)

    def _make_thumb(self, b64: str) -> QLabel:
        try:
            data = base64.b64decode(b64)
            byte_array = QByteArray(data)
            
            # Use QMovie for potentially animated images (GIF)
            buffer = QBuffer(byte_array)
            buffer.open(QBuffer.OpenModeFlag.ReadOnly)
            movie = QMovie(buffer)
            movie.setParent(self) # Keep buffer/movie alive
            
            label = QLabel()
            label.setStyleSheet(f"border: 1px solid {BORDER}; border-radius: 4px;")
            
            if movie.isValid() and movie.frameCount() > 1:
                # It's an animated image. 
                # We need to preserve the aspect ratio, which QMovie doesn't handle natively 
                # in setScaledSize easily. We'll let it play at original size or scale 
                # if we really want to, but for now let's just show it.
                movie.setScaledSize(Qt.Size(160, 120)) 
                label.setMovie(movie)
                movie.start()
                self._movies.append(movie)
            else:
                pix = QPixmap()
                pix.loadFromData(data)
                scaled = pix.scaled(
                    160, 120,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                label.setPixmap(scaled)
            
            return label
        except Exception as exc:
            label = QLabel(f"[image: {exc}]")
            label.setStyleSheet(f"color: {DANGER};")
            return label
