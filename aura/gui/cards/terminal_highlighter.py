"""Semantic pattern-based highlighter for terminal output.

This is NOT a full parser or lexer — it applies colour formatting based on
simple line-level patterns (command prefix, keywords) to make terminal output
more scannable at a glance.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from aura.gui.theme import ACCENT, DANGER, FG_DIM, SUCCESS, WARN


class TerminalHighlighter(QSyntaxHighlighter):
    """Semantic pattern-based highlighter for terminal output.

    Applies colour formatting per line:
    - Lines starting with ``$ `` (commands) → accent colour + bold.
    - Lines mentioning "error", "failed", "fail", or "✗" → danger colour.
    - Lines mentioning "success", "done", or "✓" → success colour.
    - Lines mentioning "warning" or "warn" → warn colour.
    - Everything else → dim foreground.

    This is a lightweight, pattern-based approach — no caching, no lexer,
    just per-line matching for fast, scannable output.
    """

    def __init__(self, parent) -> None:
        super().__init__(parent)

        # Pre-build formats for speed
        self._cmd_fmt = QTextCharFormat()
        self._cmd_fmt.setForeground(QColor(ACCENT))
        self._cmd_fmt.setFontWeight(QFont.Weight.Bold)

        self._danger_fmt = QTextCharFormat()
        self._danger_fmt.setForeground(QColor(DANGER))

        self._success_fmt = QTextCharFormat()
        self._success_fmt.setForeground(QColor(SUCCESS))

        self._warn_fmt = QTextCharFormat()
        self._warn_fmt.setForeground(QColor(WARN))

        self._dim_fmt = QTextCharFormat()
        self._dim_fmt.setForeground(QColor(FG_DIM))

    def highlightBlock(self, text: str) -> None:
        """Apply semantic highlighting to a single line of terminal output."""
        if not text:
            return

        # Command line — highest priority
        if text.startswith("$ "):
            self.setFormat(0, len(text), self._cmd_fmt)
            return

        lower = text.lower()

        # Error / failure keywords
        if "error" in lower or "failed" in lower or "fail" in lower or "✗" in text:
            self.setFormat(0, len(text), self._danger_fmt)
            return

        # Success keywords
        if "success" in lower or "✓" in text or "done" in lower:
            self.setFormat(0, len(text), self._success_fmt)
            return

        # Warning keywords
        if "warning" in lower or "warn" in lower:
            self.setFormat(0, len(text), self._warn_fmt)
            return

        # Default: dim
        self.setFormat(0, len(text), self._dim_fmt)
