"""Read-only card displaying a single syntax-highlighted code block."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QToolButton, QVBoxLayout, QApplication, QWidget

from aura.gui.cards._helpers import _HAVE_PYGMENTS, _mono_font
from aura.gui.syntax import PygmentsHighlighter
from aura.gui.theme import BG, BG_ALT, BORDER, FG_DIM, FG, BG_RAISED


class CodeBlockCard(QFrame):
    """Read-only card displaying a single syntax-highlighted code block."""

    def __init__(self, language: str, code: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("codeBlockCard")
        self._code = code
        # Subtle card styling — distinct background, rounded border
        self.setStyleSheet(
            f"QFrame#codeBlockCard {{ background: {BG}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Language header bar
        header_row = QWidget()
        header_row.setStyleSheet(
            f"background: {BG_ALT}; border-top-left-radius: 6px; "
            f"border-top-right-radius: 6px; border-bottom: 1px solid {BORDER};"
        )
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(10, 3, 4, 3)
        header_layout.setSpacing(0)

        lang_display = language if language else "code"
        header_lbl = QLabel(lang_display.upper())
        header_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            f"font-size: 10px; font-weight: 700;"
        )
        header_layout.addWidget(header_lbl)
        header_layout.addStretch(1)

        self._copy_btn = QToolButton()
        self._copy_btn.setText("Copy")
        self._copy_btn.setStyleSheet(
            f"QToolButton {{ color: {FG_DIM}; border: none; border-radius: 3px; "
            f"padding: 2px 6px; font-size: 10px; font-weight: 600; }} "
            f"QToolButton:hover {{ background: {BG_RAISED}; color: {FG}; }}"
        )
        self._copy_btn.clicked.connect(self._on_copy)
        header_layout.addWidget(self._copy_btn)

        layout.addWidget(header_row)

        # Code view
        code_view = QPlainTextEdit()
        code_view.setReadOnly(True)
        code_view.setFont(_mono_font(10))
        code_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        code_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; border: none; "
            f"padding: 8px; border-radius: 4px; }}"
        )
        code_view.setPlainText(code)
        
        # Auto-size to content
        doc = code_view.document()
        doc.setDocumentMargin(2)
        doc_height = int(doc.size().height() + 16)
        code_view.setFixedHeight(max(80, min(doc_height, 600)))
        
        layout.addWidget(code_view)

        # Attach native syntax highlighter — must be stored as an instance
        # attribute to prevent Python GC from destroying the highlightBlock override.
        self._highlighter = None
        if _HAVE_PYGMENTS:
            self._highlighter = PygmentsHighlighter(code_view.document(), language)

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self._code)
        self._copy_btn.setText("Copied!")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2000, lambda: self._copy_btn.setText("Copy"))
