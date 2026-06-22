"""Collapsible card showing streaming terminal output from run_terminal_command."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QPlainTextEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards._helpers import _mono_font
from aura.gui.cards.terminal_highlighter import TerminalHighlighter
from aura.gui.theme import ACCENT, BG, BORDER, DANGER, FG, SUCCESS, TERMINAL_BG, WARN


class TerminalCard(QFrame):
    """Collapsible card showing streaming terminal output from run_terminal_command.

    Header: "$ command" with state indicator: (running), (done ✓), (failed ✗)
    Body: dark monospace output area that auto-scrolls.
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(self, command: str, parent=None, start_collapsed: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("terminalCard")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._command = command
        self._state = self.STATE_RUNNING

        self._pending = ""
        self._dirty = False

        self.setStyleSheet(
            f"QFrame#terminalCard {{"
            f"  background: {TERMINAL_BG};"
            f"  border: 1px solid rgba(255, 255, 255, 0.06);"
            f"  border-left: 3px solid rgba(255, 255, 255, 0.08);"
            f"  border-radius: 8px;"
            f"}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        # Header toggle
        self._header = QToolButton(self)
        self._header.setObjectName("sectionToggle")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setMinimumWidth(0)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        # Body: output view
        self._body = QWidget(self)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._output_view = QPlainTextEdit(self._body)
        self._output_view.setReadOnly(True)
        self._output_view.setMinimumSize(0, 0)
        self._output_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._output_view.setFont(_mono_font(9))
        self._output_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._output_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._output_view.setStyleSheet(
            f"background: {TERMINAL_BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 8px; "
            "font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
            f"selection-background-color: {ACCENT}; selection-color: {BG};"
        )
        body_layout.addWidget(self._output_view)

        # Prevent unbounded document growth that slows rendering
        self._output_view.document().setMaximumBlockCount(4000)

        self._body.setVisible(not start_collapsed)
        layout.addWidget(self._body)

        # Attach semantic highlighter
        self._highlighter = TerminalHighlighter(self)
        self._highlighter.setDocument(self._output_view.document())

        self._refresh_header()

        # Throttle output updates to ~30fps to keep the GUI responsive
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flush)
        self._timer.setInterval(33)  # ~30 fps
        self._timer.start()

        # Coalesce auto-scroll to avoid stutter during rapid flushes
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._do_scroll)

    def _toggle_body(self) -> None:
        self._set_body_visible(not self._body.isVisible())
        self._refresh_header()

    def _set_body_visible(self, visible: bool) -> None:
        self._body.setVisible(visible)
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()

    def _refresh_header(self) -> None:
        chev = "v" if self._body.isVisible() else ">"
        state_str = {
            self.STATE_RUNNING: "(running)",
            self.STATE_DONE: "(done ✓)",
            self.STATE_FAILED: "(failed ✗)",
        }[self._state]
        state_color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS,
            self.STATE_FAILED: DANGER,
        }[self._state]
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{"
            f"  color: {state_color};"
            f"  font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
            f"  font-weight: 600;"
            f"}}"
        )
        prefix = f"{chev}  $ "
        suffix = f"  {state_str}"
        metrics = QFontMetrics(self._header.font())
        available = max(40, self._header.width() - 10)
        command_width = max(24, available - metrics.horizontalAdvance(prefix + suffix))
        command = metrics.elidedText(
            self._command or "Terminal",
            Qt.TextElideMode.ElideRight,
            command_width,
        )
        self._header.setText(f"{prefix}{command}{suffix}")
        self._header.setToolTip(f"$ {self._command or 'Terminal'}")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_header()

    def set_command(self, command: str) -> None:
        """Update the command shown in the header."""
        if command and command != "...":
            self._command = command
            self._refresh_header()

    def append_output(self, text: str) -> None:
        """Append a chunk of stdout/stderr text (buffered, flushed at 30fps)."""
        self._pending += text
        self._dirty = True

    def _flush(self) -> None:
        """Flush the pending output buffer to the QPlainTextEdit at most 30fps."""
        if not self._dirty:
            return
        self._dirty = False
        self._output_view.insertPlainText(self._pending)
        self._pending = ""
        # Coalesced auto-scroll: defer the scrollbar work
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _do_scroll(self) -> None:
        """Scroll the output view to the bottom."""
        sb = self._output_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_result(self, exit_code: int) -> None:
        """Set the final state based on the exit code."""
        self._flush()  # Flush any buffered output before stopping the timer
        # Force final scroll to show last output
        self._scroll_timer.stop()
        self._do_scroll()
        self._timer.stop()
        self._state = self.STATE_DONE if exit_code == 0 else self.STATE_FAILED
        if exit_code != 0:
            # Auto-expand on failure
            self._set_body_visible(True)
        else:
            self._set_body_visible(False)
        self._refresh_header()

    def expand(self) -> None:
        """Expand the card body to reveal terminal output."""
        self._set_body_visible(True)
        self._refresh_header()

    def collapse(self) -> None:
        """Collapse the card body."""
        self._set_body_visible(False)
        self._refresh_header()
