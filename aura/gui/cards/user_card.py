"""User message card with optional image thumbnails."""
from __future__ import annotations

import base64

from PySide6.QtCore import QBuffer, QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QMovie, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from aura.config import media_path
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import BG_RAISED, BORDER, DANGER, FG_BODY_USER


class UserCard(QFrame):
    def __init__(self, text: str, image_b64s: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("userCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        self._text = text

        # Header row with "You" label and copy button
        header_row = QWidget(self)
        header_row.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        header = QLabel("You", parent=header_row)
        header.setObjectName("userHeader")
        header_layout.addWidget(header)

        header_layout.addStretch(1)

        self._copy_btn = QToolButton(header_row)
        self._copy_btn.setIcon(QIcon(str(media_path("copy-classic.svg"))))
        self._copy_btn.setIconSize(QSize(16, 16))
        self._copy_btn.setToolTip("Copy message")
        self._copy_btn.setStyleSheet(
            f"QToolButton {{ border: none; border-radius: 3px; padding: 2px; }} "
            f"QToolButton:hover {{ background: {BG_RAISED}; }}"
        )
        self._copy_btn.clicked.connect(self._on_copy)
        header_layout.addWidget(self._copy_btn)

        layout.addWidget(header_row)

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
            body = QLabel(parent=self)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setText(_render_markdown_with_code(text, color=FG_BODY_USER))
            # The QLabel stylesheet color is now redundant but kept for safety.
            body.setStyleSheet(f"color: {FG_BODY_USER};")
            body.setMinimumWidth(0)
            body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
            
            label = QLabel(parent=self)
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
            label = QLabel(f"[image: {exc}]", parent=self)
            label.setStyleSheet(f"color: {DANGER};")
            return label

    # ---- copy button -----------------------------------------------------

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self._text)
        self._copy_btn.setIcon(QIcon())
        self._copy_btn.setText("\u2713")
        self._copy_btn.setToolTip("Copied!")
        QTimer.singleShot(2000, self._reset_copy_btn)

    def _reset_copy_btn(self) -> None:
        self._copy_btn.setIcon(QIcon(str(media_path("copy-classic.svg"))))
        self._copy_btn.setText("")
        self._copy_btn.setToolTip("Copy message")
