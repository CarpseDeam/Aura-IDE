"""Shared constants and utility functions for chat cards."""
from __future__ import annotations

import html as _html
import re

from PySide6.QtCore import QEasingCurve
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

try:
    from pygments import highlight  # noqa: F401
    from pygments.formatters import HtmlFormatter  # noqa: F401
    from pygments.lexers import TextLexer, get_lexer_by_name  # noqa: F401
    from pygments.util import ClassNotFound  # noqa: F401
    from aura.gui.syntax import PygmentsHighlighter, DiffHighlighter, language_from_path  # noqa: F401
    _HAVE_PYGMENTS = True
except ImportError:  # pragma: no cover — declared in pyproject, but soft-fail.
    _HAVE_PYGMENTS = False


_CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+\-.]*)\n(.*?)(?:```|\Z)", re.DOTALL)


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

    anim = QPropertyAnimation(effect, b"opacity")
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # Clean up the effect after animation completes so it doesn't interfere
    # with sub-widget rendering (QPlainTextEdit etc.)
    def _cleanup():
        try:
            if widget is not None:
                widget.setGraphicsEffect(None)
            effect.deleteLater()
            anim.deleteLater()
        except RuntimeError:
            pass  # C++ object already deleted (widget/effect cleaned up by parent deletion)
    anim.finished.connect(_cleanup)
    anim.start()
