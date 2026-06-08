"""QR code rendering widget — pure-Qt rasterizer with optional qrcode lib fallback.

Tries to use the `qrcode` package if available (high-quality output). If not,
shows a friendly placeholder so the manual pairing code is still usable.
"""
from __future__ import annotations

import io
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy

logger = logging.getLogger(__name__)


class QrCodeLabel(QLabel):
    """QLabel that renders a QR code from a string of data."""

    def __init__(self, size: int = 220, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "background: #ffffff; border-radius: 12px; padding: 6px;"
            " border: 1px solid #2a2a35;"
        )

    def set_data(self, data: str) -> None:
        if not data:
            self.clear()
            self.setText("")
            return
        pixmap = _render_qr_pixmap(data, self._size - 12)
        if pixmap is None:
            self.setStyleSheet(
                "background: #1c1c22; color: #a8aebb; border: 1px dashed #2e3340;"
                " border-radius: 12px; padding: 12px;"
            )
            self.setText("Install `qrcode` to render the QR\nUse the code below to pair manually.")
            return
        self.setPixmap(pixmap)


def _render_qr_pixmap(data: str, size: int) -> QPixmap | None:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:
        logger.warning("qrcode package not available — manual pairing only")
        return None
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#000000", back_color="#ffffff")
        pil = img.get_image() if hasattr(img, "get_image") else img
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        qimage = QImage.fromData(buf.getvalue(), "PNG")
        if qimage.isNull():
            return None
        pixmap = QPixmap.fromImage(qimage)
        return pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    except Exception as exc:
        logger.error("QR render failed: %s", exc)
        return None
