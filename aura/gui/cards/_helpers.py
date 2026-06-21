"""Shared constants and utility functions for chat cards."""
from __future__ import annotations

import html as _html
import re

from PySide6.QtCore import QEasingCurve, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QSizePolicy, QTextBrowser, QWidget

# Use the ones from markdown_renderer to avoid circularity if markdown_renderer needs them
from aura.gui.markdown_renderer import _CODE_FENCE_RE, _HAVE_PYGMENTS


class _MarkdownTextBlock(QTextBrowser):
    """Auto-height rich text block for finalized markdown."""

    def __init__(self, html: str, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenExternalLinks(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("background: transparent; border: none;")
        self.document().setDocumentMargin(0)
        self.setHtml(html)
        self._sync_height()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def _sync_height(self) -> None:
        width = max(1, self.viewport().width())
        self.document().setTextWidth(width)
        height = int(self.document().size().height() + 4)
        self.setFixedHeight(max(1, height))


def _wrap_body_text(text: str, color: str) -> str:
    """Escape plain text and wrap it in a div with explicit color and a comfortable
    line-height — QLabel ignores QSS line-height, so rich-text wrapping is the only way.
    """
    escaped = _html.escape(text).replace("\n", "<br/>")
    return f'<div style="color: {color}; line-height: 145%;">{escaped}</div>'


def _mono_font(pt: int = 10) -> QFont:
    f = QFont("Geist Mono, JetBrains Mono, Consolas, Menlo, monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    f.setPointSize(pt)
    return f


def _fade_in_widget(widget: QWidget, duration: int = 150) -> None:
    """Apply a fade-in opacity animation to a newly-added widget."""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    effect.setOpacity(0.0)

    from PySide6.QtCore import QPropertyAnimation

    anim = QPropertyAnimation(effect, b"opacity", parent=effect)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # Clean up the effect after animation completes so it doesn't interfere
    # with sub-widget rendering (QPlainTextEdit etc.)
    def _cleanup():
        try:
            # Check if C++ object still exists
            if effect is not None:
                # If widget still exists, remove effect
                if widget is not None:
                    try:
                        widget.setGraphicsEffect(None)
                    except (RuntimeError, AttributeError):
                        pass
                effect.deleteLater()
        except (RuntimeError, AttributeError):
            pass

    anim.finished.connect(_cleanup)
    anim.start()
