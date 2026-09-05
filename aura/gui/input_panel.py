"""Bottom input panel: textarea, attachments, send/stop."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImage, QPixmap, QTextOption
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

from aura.gui.composer_skills import ComposerSkill, ComposerSkillsWidget
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
    selected_skills: tuple[ComposerSkill, ...] = ()


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
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
    skills_requested = Signal()
    #: Emitted whenever the unsent installed-skill chips change, so the Skills
    #: manager can keep its "already added" state in step with the composer.
    skill_selection_changed = Signal()

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
        self._execution_active = False
        self._queued_count = 0
        self._attachments: list[Attachment] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(6)

        # Explicit installed skills are independently owned by this focused
        # widget; InputPanel only captures/restores its immutable selection.
        self._composer_skills = ComposerSkillsWidget()
        self._composer_skills.selection_changed.connect(self.skill_selection_changed.emit)
        outer.addWidget(self._composer_skills)

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
        if workspace_root is not None:
            self._editor.setPlaceholderText(
                "Describe the bug, paste a screenshot (Ctrl+V), or drop files here. "
                "Ctrl+Enter to send."
            )
        else:
            self._editor.setPlaceholderText("Choose a project folder first.")
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
        self._editor.textChanged.connect(self._update_send_button_enabled)

        # Controls row.
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self._skills_btn = QPushButton("Skills")
        self._skills_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skills_btn.setToolTip("Browse skills and add them to your next message.")
        self._skills_btn.setEnabled(workspace_root is not None)
        self._skills_btn.clicked.connect(self.skills_requested.emit)
        controls.addWidget(self._skills_btn)

        controls.addStretch(1)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setMinimumSize(44, 36)
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        controls.addWidget(self._stop_btn)

        self._send_btn = QPushButton("→")
        self._send_btn.setObjectName("sendButton")
        self._send_btn.setMinimumSize(32, 30)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setToolTip("Send message (Ctrl+Enter)")
        self._send_btn.setStyleSheet(
            "QPushButton#sendButton {"
            "  background: #4a9eff;"
            "  color: white;"
            "  border: none;"
            "  border-radius: 8px;"
            "  font-size: 14px;"
            "  font-weight: bold;"
            "  padding: 1px 8px;"
            "  min-width: 24px;"
            "  min-height: 28px;"
            "}"
            "QPushButton#sendButton:hover {"
            "  background: #6ab0ff;"
            "}"
            "QPushButton#sendButton:pressed {"
            "  background: #3a8ee0;"
            "}"
            "QPushButton#sendButton:disabled {"
            "  background: rgba(74,158,255,0.25);"
            "  color: rgba(255,255,255,0.3);"
            "}"
            "QPushButton#sendButton:focus {"
            "  border: 2px solid rgba(255,255,255,0.6);"
            "}"
        )
        self._send_btn.clicked.connect(self._on_submit)
        controls.addWidget(self._send_btn)
        self._update_send_button_enabled()

        outer.addLayout(controls)

    # ---- public state -----------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        if root != self._workspace_root:
            self._composer_skills.clear()
        self._workspace_root = root
        self._skills_btn.setEnabled(root is not None)

    def select_installed_skill(self, install_id: str, label: str) -> bool:
        """Select an installed skill by stable identity for the next send."""
        return self._composer_skills.select_installed_skill(install_id, label)

    def remove_selected_skill(self, install_id: str) -> bool:
        """Drop one selected identity's unsent chip, leaving the rest alone."""
        return self._composer_skills.remove_installed_skill(install_id)

    def clear_selected_skills(self) -> None:
        """Clear unsent installed-skill chips."""
        self._composer_skills.clear()

    def restore_selected_skills(self, skills: tuple[ComposerSkill, ...]) -> None:
        """Put back a captured selection the send did not actually consume."""
        self._composer_skills.restore(tuple(skills))

    def selected_skills(self) -> tuple[ComposerSkill, ...]:
        """Return the current immutable ordered selection."""
        return self._composer_skills.selection

    def set_execution_active(self, active: bool) -> None:
        """Set whether a production run is active.

        When active, the editor stays editable and a Queue button is shown
        alongside the Stop button. When idle, a normal Send button is shown.
        """
        self._execution_active = active
        self._stop_btn.setVisible(active)
        self._update_send_button_text()
        self._send_btn.setVisible(True)

    def set_placeholder(self, text: str) -> None:
        """Set the editor placeholder text."""
        self._editor.setPlaceholderText(text)

    def set_queued_messages(self, count: int) -> None:
        """Update the send button to show how many messages are queued."""
        self._queued_count = count
        self._update_send_button_text()

    def _update_send_button_text(self) -> None:
        """Update send button label and tooltip based on state."""
        if self._execution_active and self._queued_count > 0:
            self._send_btn.setText(f"Queue \u00b7 {self._queued_count}")
            self._send_btn.setToolTip(
                f"{self._queued_count} message(s) queued — will send after current run completes"
            )
        elif self._execution_active:
            self._send_btn.setText("Queue")
            self._send_btn.setToolTip("Queue message — sends after current run completes")
        else:
            self._send_btn.setText("Send")
            self._send_btn.setToolTip("Send message (Ctrl+Enter)")

    def _update_send_button_enabled(self) -> None:
        self._send_btn.setEnabled(
            bool(self._editor.toPlainText().strip()) or bool(self._attachments)
        )

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
        self._update_send_button_enabled()

    def _remove_chip(self, chip: _AttachmentChip) -> None:
        try:
            self._attachments.remove(chip.attachment)
        except ValueError:
            pass
        chip.deleteLater()
        if not self._attachments:
            self._chips_container.setVisible(False)
        self._update_send_button_enabled()

    def _clear_attachments(self) -> None:
        self._attachments.clear()
        while self._chips_row.count() > 1:
            item = self._chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._chips_container.setVisible(False)
        self._update_send_button_enabled()

    # ---- slash hint -------------------------------------------------------

    def _update_slash_hint(self) -> None:
        text = self._editor.toPlainText()
        if not text.startswith("/"):
            self._slash_hint.setVisible(False)
            return

        normalized = text.strip().lower()
        if normalized == "/agents":
            self._slash_hint.setText(
                "/agents opens the Agents page."
            )
        elif normalized.startswith("/agents "):
            self._slash_hint.setText(
                "Use /agents by itself to open the Agents page."
            )
        elif normalized == "/skills":
            self._slash_hint.setText(
                "/skills opens the Skills manager."
            )
        elif normalized.startswith("/skills "):
            self._slash_hint.setText(
                "Use /skills by itself to open the Skills manager."
            )
        else:
            self._slash_hint.setText(
                "/agents  —  Open the Agents page.        /skills  —  Browse skills."
            )
        self._slash_hint.setVisible(True)

    # ---- send -------------------------------------------------------------

    def _on_submit(self) -> None:
        text = self._editor.toPlainText().strip()
        if not text and not self._attachments:
            return
        payload = SendPayload(
            text=text,
            attachments=list(self._attachments),
            selected_skills=self._composer_skills.selection,
        )
        self._editor.clear()
        self._clear_attachments()
        self._composer_skills.clear()
        self.sent.emit(payload)

    def set_text(self, text: str) -> None:
        """Set the editor text, replacing any current content."""
        self._editor.setPlainText(text)
        self._editor.setFocus()

    def suggest_text(self, text: str) -> None:
        """Offer a starting point while preserving an existing composer draft."""
        if not self._editor.toPlainText().strip():
            self._editor.setPlainText(text)
        self.focus_editor()

    def set_attachments(self, attachments: list[Attachment]) -> None:
        """Restore a list of attachments to the panel."""
        self._clear_attachments()
        for a in attachments:
            self._add_attachment(a)

    def restore_payload(self, payload: SendPayload) -> None:
        """Restore a rejected payload, including its installed-skill chips."""
        self.set_text(payload.text)
        self.set_attachments(payload.attachments)
        self._composer_skills.restore(payload.selected_skills)

    def focus_editor(self) -> None:
        self._editor.setFocus()
