"""Bottom input panel: textarea, attachments, send/stop."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap, QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import media_path
from aura.gui.theme import BG_RAISED, BORDER, DANGER, FG, FG_DIM

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


@dataclass
class Attachment:
    kind: str  # "image" or "file"
    name: str
    b64: str | None  # for images
    text_ref: str | None  # for files: "[user attached: rel/path]"

    def thumb_pixmap(self) -> QPixmap | None:
        if self.kind != "image" or self.b64 is None:
            return None
        pix = QPixmap()
        pix.loadFromData(base64.b64decode(self.b64))
        return pix


@dataclass
class SendPayload:
    text: str
    attachments: list[Attachment]


class _AttachmentChip(QFrame):
    removed = Signal(object)  # emits self

    def __init__(self, attachment: Attachment) -> None:
        super().__init__()
        self.attachment = attachment
        self.setStyleSheet(
            f"QFrame {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 2px 6px; }} "
            f"QFrame:hover {{ background: {BORDER}; border-color: {FG_DIM}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        pix = attachment.thumb_pixmap()
        if pix is not None:
            thumb = QLabel()
            thumb.setPixmap(
                pix.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )
            layout.addWidget(thumb)

        label = QLabel(attachment.name)
        label.setStyleSheet(f"color: {FG};")
        layout.addWidget(label)

        close = QToolButton()
        close.setText("x")
        close.setStyleSheet(f"QToolButton {{ background: transparent; color: {FG_DIM}; border: none; }} "
                            f"QToolButton:hover {{ color: {DANGER}; }}")
        close.clicked.connect(lambda: self.removed.emit(self))
        layout.addWidget(close)


class _AutoGrowTextEdit(QTextEdit):
    """Multiline edit that auto-grows up to a maximum number of lines."""

    submitted = Signal()
    image_pasted = Signal(QImage)
    files_dropped = Signal(list)  # list[Path]

    MAX_LINES = 8

    def __init__(self) -> None:
        super().__init__()
        self.setPlaceholderText(
            "Describe the bug, paste a screenshot (Ctrl+V), or drop files here. "
            "Ctrl+Enter to send."
        )
        self.setAcceptRichText(False)
        self.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.document().contentsChanged.connect(self._adjust_height)
        self._adjust_height()

    def _adjust_height(self) -> None:
        line_h = self.fontMetrics().lineSpacing()
        # Margins + 1 line min, MAX_LINES max.
        doc_h = int(self.document().size().height())
        target = min(line_h * self.MAX_LINES, max(line_h, doc_h)) + 14
        self.setFixedHeight(target)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
                self.submitted.emit()
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self.image_pasted.emit(img)
                return
        # Paste plain text only.
        if source.hasText():
            self.insertPlainText(source.text())

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        md = event.mimeData()
        if md.hasUrls():
            paths: list[Path] = []
            for url in md.urls():
                if url.isLocalFile():
                    paths.append(Path(url.toLocalFile()))
            if paths:
                self.files_dropped.emit(paths)
                event.acceptProposedAction()
                return
        if md.hasImage():
            img = md.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self.image_pasted.emit(img)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class InputPanel(QFrame):
    """Composer at the bottom of the window."""

    sent = Signal(SendPayload)
    stop_requested = Signal()
    retry_requested = Signal()
    handoff_requested = Signal()

    def __init__(self, workspace_root: Path | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame {"
            "  background: rgba(34, 34, 40, 0.85);"
            "  border: 1px solid rgba(255, 255, 255, 0.08);"
            "  border-radius: 18px;"
            "}"
        )
        # Drop shadow for floating pill effect
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 180))
        self.setGraphicsEffect(shadow)

        self._workspace_root = workspace_root
        self._streaming = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(6)

        # Attachment chips row (hidden when empty).
        self._chips_row = QHBoxLayout()
        self._chips_row.setContentsMargins(0, 0, 0, 0)
        self._chips_row.setSpacing(6)
        self._chips_row.addStretch(1)
        self._chips_container = QWidget()
        self._chips_container.setLayout(self._chips_row)
        self._chips_container.setVisible(False)
        self._chips_container.setStyleSheet("background: transparent;")
        outer.addWidget(self._chips_container)

        # Editor.
        self._editor = _AutoGrowTextEdit()
        self._editor.submitted.connect(self._on_submit)
        self._editor.image_pasted.connect(self._on_image_pasted)
        self._editor.files_dropped.connect(self._on_files_dropped)
        outer.addWidget(self._editor)

        # Slash command hint
        self._slash_hint = QLabel()
        self._slash_hint.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; padding: 2px 12px; background: transparent;"
        )
        self._slash_hint.setWordWrap(True)
        self._slash_hint.setVisible(False)
        outer.addWidget(self._slash_hint)

        # Connect text changed for slash hint
        self._editor.textChanged.connect(self._update_slash_hint)

        # Controls row.
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self._handoff_btn = QToolButton()
        self._handoff_btn.setIcon(QIcon(str(media_path("move_group.svg"))))
        self._handoff_btn.setToolTip("Generate a handoff and continue in a fresh chat")
        self._handoff_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._handoff_btn.clicked.connect(self.handoff_requested.emit)
        controls.addWidget(self._handoff_btn)

        controls.addStretch(1)

        self._retry_btn = QToolButton()
        self._retry_btn.setText("↻")
        self._retry_btn.setToolTip("Retry last message")
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.clicked.connect(self.retry_requested.emit)
        controls.addWidget(self._retry_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        controls.addWidget(self._stop_btn)

        self._send_btn = QPushButton("→")
        self._send_btn.setObjectName("primary")
        font = self._send_btn.font()
        font.setPointSize(14)
        self._send_btn.setFont(font)
        self._send_btn.setStyleSheet(
            "QPushButton#primary { padding: 5px 14px; font-size: 16px; }"
        )
        self._send_btn.clicked.connect(self._on_submit)
        controls.addWidget(self._send_btn)

        outer.addLayout(controls)

        self._attachments: list[Attachment] = []

        # Saved originals for drone architect mode restoration
        self._original_placeholder = self._editor.placeholderText()
        self._original_send_text = self._send_btn.text()
        self._original_send_tooltip = self._send_btn.toolTip()
        self._original_frame_style = self.styleSheet()
        self._drone_architect_active = False

    # ---- public state -----------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root

    def set_streaming(self, streaming: bool) -> None:
        self._streaming = streaming
        self._send_btn.setVisible(not streaming)
        self._stop_btn.setVisible(streaming)
        self._handoff_btn.setEnabled(not streaming)
        self._retry_btn.setEnabled(not streaming)
        self._editor.setEnabled(not streaming)

    def set_placeholder(self, text: str) -> None:
        """Set the editor placeholder text."""
        self._editor.setPlaceholderText(text)

    def set_drone_architect_mode(self, active: bool) -> None:
        """Toggle the drone architect visual state on the input panel."""
        self._drone_architect_active = active
        if active:
            self._editor.setPlaceholderText(
                "Describe the Drone you want to build..."
            )
            self._send_btn.setText("Forge")
            self._send_btn.setToolTip("Forge Drone")
            self.setStyleSheet(
                "QFrame {"
                "  background: rgba(34, 34, 40, 0.85);"
                "  border: 1px solid rgba(157, 124, 216, 0.5);"
                "  border-radius: 18px;"
                "}"
            )
        else:
            self._editor.setPlaceholderText(self._original_placeholder)
            self._send_btn.setText(self._original_send_text)
            self._send_btn.setToolTip(self._original_send_tooltip)
            self.setStyleSheet(self._original_frame_style)

    def set_queued_messages(self, count: int) -> None:
        """Update the send button to show how many messages are queued."""
        if count > 0:
            self._send_btn.setText(f"→  [{count} queued]")
        else:
            if self._drone_architect_active:
                self._send_btn.setText("Forge")
            else:
                self._send_btn.setText("→")

    # ---- attachments ------------------------------------------------------

    def _on_image_pasted(self, qimg: QImage) -> None:
        # Convert QImage -> PNG bytes -> base64.
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        qimg.save(buf, "PNG")
        buf.close()
        b64 = base64.b64encode(bytes(ba)).decode("ascii")
        self._add_attachment(Attachment(kind="image", name="pasted.png", b64=b64, text_ref=None))

    def _on_files_dropped(self, paths: list[Path]) -> None:
        for p in paths:
            if not p.exists():
                continue
            if p.suffix.lower() in IMAGE_SUFFIXES:
                try:
                    with Image.open(p) as im:
                        im = im.convert("RGB") if im.mode in ("P", "CMYK") else im
                        out = io.BytesIO()
                        im.save(out, format="PNG")
                        b64 = base64.b64encode(out.getvalue()).decode("ascii")
                    self._add_attachment(
                        Attachment(kind="image", name=p.name, b64=b64, text_ref=None)
                    )
                except Exception as exc:
                    self._add_attachment(
                        Attachment(
                            kind="file",
                            name=p.name,
                            b64=None,
                            text_ref=f"[user attached image but it could not be read: {p.name} ({exc})]",
                        )
                    )
            else:
                rel = self._relpath(p)
                self._add_attachment(
                    Attachment(kind="file", name=rel, b64=None, text_ref=f"[user attached: {rel}]")
                )

    def _relpath(self, p: Path) -> str:
        if self._workspace_root is None:
            return str(p)
        try:
            return p.resolve().relative_to(self._workspace_root.resolve()).as_posix()
        except ValueError:
            return str(p)

    def _add_attachment(self, a: Attachment) -> None:
        self._attachments.append(a)
        chip = _AttachmentChip(a)
        chip.removed.connect(self._remove_chip)
        # Insert before stretch.
        self._chips_row.insertWidget(self._chips_row.count() - 1, chip)
        self._chips_container.setVisible(True)

    def _remove_chip(self, chip: _AttachmentChip) -> None:
        try:
            self._attachments.remove(chip.attachment)
        except ValueError:
            pass
        chip.deleteLater()
        if not self._attachments:
            self._chips_container.setVisible(False)

    def _clear_attachments(self) -> None:
        self._attachments.clear()
        while self._chips_row.count() > 1:
            item = self._chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._chips_container.setVisible(False)

    # ---- slash hint -------------------------------------------------------

    def _update_slash_hint(self) -> None:
        text = self._editor.toPlainText()
        if not text.startswith("/"):
            self._slash_hint.setVisible(False)
            return

        normalized = text.strip().lower()
        if normalized == "/drone":
            self._slash_hint.setText(
                "/drone opens Drone Builder. Describe the Drone you want to build."
            )
        elif normalized.startswith("/drone "):
            self._slash_hint.setText(
                "Use /drone by itself, then describe the Drone in the next message."
            )
        else:
            self._slash_hint.setText(
                "/drone  —  Open Drone Builder."
            )
        self._slash_hint.setVisible(True)

    # ---- send -------------------------------------------------------------

    def _on_submit(self) -> None:
        if self._streaming:
            return
        text = self._editor.toPlainText().strip()
        if not text and not self._attachments:
            return
        payload = SendPayload(text=text, attachments=list(self._attachments))
        self._editor.clear()
        self._clear_attachments()
        self.sent.emit(payload)

    def set_text(self, text: str) -> None:
        """Set the editor text, replacing any current content."""
        self._editor.setPlainText(text)
        self._editor.setFocus()

    def set_attachments(self, attachments: list[Attachment]) -> None:
        """Restore a list of attachments to the panel."""
        self._clear_attachments()
        for a in attachments:
            self._add_attachment(a)

    def restore_payload(self, payload: SendPayload) -> None:
        """Restore a previously submitted payload to the editor and attachments."""
        self.set_text(payload.text)
        self.set_attachments(payload.attachments)

    def focus_editor(self) -> None:
        self._editor.setFocus()
