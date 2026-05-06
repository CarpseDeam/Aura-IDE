"""Bottom input panel: textarea, attachments, model picker, thinking, send/stop."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut, QTextOption
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import DEFAULT_MODEL, DEFAULT_THINKING, MODELS, ModelId, ThinkingMode
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
            "border-radius: 4px; padding: 2px 6px; }}"
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
    model_changed = Signal(str)  # ModelId (planner)
    thinking_changed = Signal(str)  # ThinkingMode (planner)
    worker_model_changed = Signal(str)
    worker_thinking_changed = Signal(str)

    def __init__(self, workspace_root: Path | None) -> None:
        super().__init__()
        self.setStyleSheet(f"QFrame {{ background: {BG_RAISED}; border-top: 1px solid {BORDER}; }}")
        self._workspace_root = workspace_root
        self._streaming = False
        self._planner_worker_mode = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 10)
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

        # Controls row.
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self._planner_label = QLabel("Planner:")
        controls.addWidget(self._planner_label)
        self._model_combo = QComboBox()
        for mid, info in MODELS.items():
            self._model_combo.addItem(info.label, mid)
        self._model_combo.setCurrentIndex(
            list(MODELS.keys()).index(DEFAULT_MODEL)
        )
        self._model_combo.currentIndexChanged.connect(
            lambda _: self.model_changed.emit(self.current_model())
        )
        controls.addWidget(self._model_combo)

        self._thinking_label = QLabel("Thinking:")
        controls.addWidget(self._thinking_label)
        self._thinking_combo = QComboBox()
        self._thinking_combo.addItem("Off", "off")
        self._thinking_combo.addItem("High", "high")
        self._thinking_combo.addItem("Max", "max")
        self._thinking_combo.setCurrentIndex(
            ["off", "high", "max"].index(DEFAULT_THINKING)
        )
        self._thinking_combo.currentIndexChanged.connect(
            lambda _: self.thinking_changed.emit(self.current_thinking())
        )
        controls.addWidget(self._thinking_combo)

        # Worker controls — visible only in planner/worker mode.
        self._worker_sep = QLabel("•")
        self._worker_sep.setStyleSheet(f"color: {FG_DIM}; padding: 0 6px;")
        controls.addWidget(self._worker_sep)

        self._worker_label = QLabel("Worker:")
        controls.addWidget(self._worker_label)
        self._worker_model_combo = QComboBox()
        for mid, info in MODELS.items():
            self._worker_model_combo.addItem(info.label, mid)
        self._worker_model_combo.setCurrentIndex(
            list(MODELS.keys()).index("deepseek-v4-pro")
        )
        self._worker_model_combo.currentIndexChanged.connect(
            lambda _: self.worker_model_changed.emit(self.current_worker_model())
        )
        controls.addWidget(self._worker_model_combo)

        self._worker_thinking_label = QLabel("Thinking:")
        controls.addWidget(self._worker_thinking_label)
        self._worker_thinking_combo = QComboBox()
        self._worker_thinking_combo.addItem("Off", "off")
        self._worker_thinking_combo.addItem("High", "high")
        self._worker_thinking_combo.addItem("Max", "max")
        self._worker_thinking_combo.setCurrentIndex(
            ["off", "high", "max"].index("high")
        )
        self._worker_thinking_combo.currentIndexChanged.connect(
            lambda _: self.worker_thinking_changed.emit(self.current_worker_thinking())
        )
        controls.addWidget(self._worker_thinking_combo)

        controls.addStretch(1)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        controls.addWidget(self._stop_btn)

        self._send_btn = QPushButton("Send  Ctrl+Enter")
        self._send_btn.setObjectName("primary")
        self._send_btn.clicked.connect(self._on_submit)
        controls.addWidget(self._send_btn)

        outer.addLayout(controls)

        self._attachments: list[Attachment] = []

    # ---- public state -----------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root

    def current_model(self) -> ModelId:
        return self._model_combo.currentData()

    def current_thinking(self) -> ThinkingMode:
        return self._thinking_combo.currentData()

    def current_worker_model(self) -> ModelId:
        return self._worker_model_combo.currentData()

    def current_worker_thinking(self) -> ThinkingMode:
        return self._worker_thinking_combo.currentData()

    def set_model(self, model: ModelId) -> None:
        keys = list(MODELS.keys())
        if model in keys:
            self._model_combo.setCurrentIndex(keys.index(model))

    def set_thinking(self, thinking: ThinkingMode) -> None:
        keys = ["off", "high", "max"]
        if thinking in keys:
            self._thinking_combo.setCurrentIndex(keys.index(thinking))

    def set_worker_model(self, model: ModelId) -> None:
        keys = list(MODELS.keys())
        if model in keys:
            self._worker_model_combo.setCurrentIndex(keys.index(model))

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        keys = ["off", "high", "max"]
        if thinking in keys:
            self._worker_thinking_combo.setCurrentIndex(keys.index(thinking))

    def set_planner_worker_mode(self, enabled: bool) -> None:
        self._planner_worker_mode = enabled
        # Hide / show worker pickers; relabel the planner picker.
        self._planner_label.setText("Planner:" if enabled else "Model:")
        for w in (
            self._worker_sep,
            self._worker_label,
            self._worker_model_combo,
            self._worker_thinking_label,
            self._worker_thinking_combo,
        ):
            w.setVisible(enabled)

    def set_streaming(self, streaming: bool) -> None:
        self._streaming = streaming
        self._send_btn.setVisible(not streaming)
        self._stop_btn.setVisible(streaming)
        self._editor.setEnabled(not streaming)

    def set_queued_messages(self, count: int) -> None:
        """Update the send button to show how many messages are queued."""
        if count > 0:
            self._send_btn.setText(f"Send  Ctrl+Enter  [{count} queued]")
        else:
            self._send_btn.setText("Send  Ctrl+Enter")

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

    def focus_editor(self) -> None:
        self._editor.setFocus()
