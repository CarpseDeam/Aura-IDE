"""Streaming label — word-wrapping label that grows as text is appended."""
from __future__ import annotations

import html as _html

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QSizePolicy

from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import FG, FG_ITALIC


class _StreamLabel(QLabel):
    """Word-wrapping label that grows as text is appended. Tokens accumulate in a
    buffer and the UI is flushed at most 30 fps to keep the GUI thread responsive
    on fast token streams."""

    def __init__(self, italic: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._italic = italic
        if italic:
            self.setObjectName("reasoning")
            self.setStyleSheet(f"color: {FG_ITALIC}; font-style: italic;")
        else:
            self.setStyleSheet(f"color: {FG};")
        # Use rich text so we can control line-height during streaming.
        self.setTextFormat(Qt.TextFormat.RichText)
        self._buf = ""
        self._dirty = False

        # Throttle: update UI at most 30fps (33ms interval)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flush)
        self._timer.setInterval(33)  # ~30 fps
        self._timer.start()

    def append(self, text: str) -> None:
        self._buf += text
        self._dirty = True
        # Don't call setText here — let the timer flush it

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if self._italic:
            escaped = _html.escape(self._buf).replace("\n", "<br/>")
            self.setText(
                f'<div style="color: {FG_ITALIC}; line-height: 145%; font-style: italic;">'
                f"{escaped}</div>"
            )
        else:
            self.setText(_render_markdown_with_code(self._buf))

    def stop_timer(self) -> None:
        try:
            self._timer.stop()
        except RuntimeError:
            # Internal C++ object might already be deleted
            pass

    def text_buffer(self) -> str:
        return self._buf
