"""Read-only card displaying a single syntax-highlighted code block."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QPlainTextEdit, QVBoxLayout

from aura.gui.cards._helpers import _HAVE_PYGMENTS, _mono_font
from aura.gui.syntax import PygmentsHighlighter
from aura.gui.theme import BG, BG_ALT, BORDER, FG_DIM


class CodeBlockCard(QFrame):
    """Read-only card displaying a single syntax-highlighted code block."""

    def __init__(self, language: str, code: str) -> None:
        super().__init__()
        self.setObjectName("codeBlockCard")
        # Subtle card styling — distinct background, rounded border
        self.setStyleSheet(
            f"QFrame#codeBlockCard {{ background: {BG}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Language header bar
        lang_display = language if language else "code"
        header = QLabel(f" {lang_display} ")
        header.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            f"font-size: 10px; padding: 3px 10px; background: {BG_ALT}; "
            f"border-top-left-radius: 6px; border-top-right-radius: 6px; "
            f"border-bottom: 1px solid {BORDER};"
        )
        layout.addWidget(header)

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
        code_view.setMinimumHeight(40)
        code_view.setMaximumHeight(400)
        code_view.document().setDocumentMargin(2)
        layout.addWidget(code_view)

        # Attach native syntax highlighter — must be stored as an instance
        # attribute to prevent Python GC from destroying the highlightBlock override.
        self._highlighter = None
        if _HAVE_PYGMENTS:
            self._highlighter = PygmentsHighlighter(code_view.document(), language)
