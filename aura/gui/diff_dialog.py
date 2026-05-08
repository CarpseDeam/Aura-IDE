"""Diff approval modal — shown for every write_file/edit_file proposal."""
from __future__ import annotations

import difflib

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.conversation.tools.registry import ApprovalDecision, ApprovalRequest

try:
    from aura.gui.syntax import DiffHighlighter, language_from_path
    _HAVE_PYGMENTS = True
except ImportError:
    _HAVE_PYGMENTS = False

from aura.gui.theme import (
    BG,
    BORDER,
    FG,
    FG_DIM,
)


def render_unified_diff(old: str, new: str, rel_path: str) -> str:
    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
        n=3,
    )
    lines = list(diff)
    # Filter out metadata lines — keep only code additions/deletions/context
    cleaned = [
        line for line in lines
        if not (line.startswith("--- ") or line.startswith("+++ ") or line.startswith("@@ "))
    ]
    return "\n".join(cleaned)


class DiffApprovalDialog(QDialog):
    """Shows the proposed change. Returns ApprovalDecision via .decision()."""

    def __init__(self, request: ApprovalRequest, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Apply change to {request.rel_path}?")
        self.setModal(True)
        self.resize(900, 640)
        self._decision = ApprovalDecision(action="reject")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QLabel(self._format_header(request))
        header.setStyleSheet(f"color: {FG}; font-weight: 600; font-size: 14px;")
        layout.addWidget(header)

        sub = QLabel(
            "New file" if request.is_new_file else "Modify existing file"
        )
        sub.setStyleSheet(f"color: {FG_DIM};")
        layout.addWidget(sub)

        self._diff_view = QPlainTextEdit(self)
        self._diff_view.setReadOnly(True)
        self._diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        mono = QFont("Geist Mono, JetBrains Mono, Consolas, Menlo, monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setFixedPitch(True)
        mono.setPointSize(10)
        self._diff_view.setFont(mono)
        self._diff_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; padding: 8px; }}"
        )
        layout.addWidget(self._diff_view, 1)

        # Attach syntax highlighter for the diff view
        if _HAVE_PYGMENTS:
            lang = language_from_path(request.rel_path) or "text"
            self._highlighter = DiffHighlighter(self._diff_view.document(), lang)

        self._populate_diff(request)

        # Checkbox row — approve all remaining writes this session
        checkbox_row = QHBoxLayout()
        checkbox_row.setSpacing(8)
        self._approve_all_checkbox = QCheckBox("Approve all remaining writes this session")
        self._approve_all_checkbox.setStyleSheet(f"color: {FG_DIM};")
        checkbox_row.addWidget(self._approve_all_checkbox)
        checkbox_row.addStretch(1)
        layout.addLayout(checkbox_row)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.addStretch(1)

        reject_all_btn = QPushButton("Reject all in this turn")
        reject_all_btn.setObjectName("danger")
        reject_all_btn.clicked.connect(self._on_reject_all)
        button_row.addWidget(reject_all_btn)

        reject_btn = QPushButton("Reject")
        reject_btn.clicked.connect(self._on_reject)
        button_row.addWidget(reject_btn)

        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("success")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._on_apply)
        button_row.addWidget(apply_btn)

        layout.addLayout(button_row)

    def _format_header(self, request: ApprovalRequest) -> str:
        verb = "Create" if request.is_new_file else "Edit"
        return f"{verb}: {request.rel_path}"

    def _populate_diff(self, request: ApprovalRequest) -> None:
        if request.is_new_file:
            text = "\n".join(f"+{line}" for line in request.new_content.splitlines())
        else:
            text = render_unified_diff(
                request.old_content, request.new_content, request.rel_path
            )
            if not text.strip():
                text = "(no textual difference)"
        self._diff_view.setPlainText(text)

    def _on_apply(self) -> None:
        if self._approve_all_checkbox.isChecked():
            self._decision = ApprovalDecision(action="approve_all")
        else:
            self._decision = ApprovalDecision(action="approve")
        self.accept()

    def _on_reject(self) -> None:
        self._decision = ApprovalDecision(action="reject")
        self.reject()

    def _on_reject_all(self) -> None:
        self._decision = ApprovalDecision(action="reject_all")
        self.reject()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._on_reject()
            return
        super().keyPressEvent(event)

    def decision(self) -> ApprovalDecision:
        return self._decision
