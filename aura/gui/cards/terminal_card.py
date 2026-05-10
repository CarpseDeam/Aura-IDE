"""Collapsible card showing streaming terminal output from run_terminal_command."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget

from aura.gui.cards._helpers import _mono_font
from aura.gui.theme import BORDER, DANGER, FG, SUCCESS, TERMINAL_BG, WARN


class TerminalCard(QFrame):
    """Collapsible card showing streaming terminal output from run_terminal_command.

    Header: "> $ command" with state indicator: (running), (done ✓), (failed ✗)
    Body: dark monospace output area that auto-scrolls.
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"

    def __init__(self, command: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("terminalCard")
        self._command = command
        self._state = self.STATE_RUNNING
        self._output_buf = ""
        self._pending = ""
        self._dirty = False

        self.setStyleSheet(
            f"QFrame#terminalCard {{"
            f"  background: {TERMINAL_BG};"
            f"  border: 1px solid rgba(255, 255, 255, 0.06);"
            f"  border-left: 3px solid {SUCCESS};"
            f"  border-radius: 8px;"
            f"}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)

        # Header toggle
        self._header = QToolButton()
        self._header.setObjectName("sectionToggle")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.clicked.connect(self._toggle_body)
        layout.addWidget(self._header)

        # Body: output view
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._output_view = QPlainTextEdit()
        self._output_view.setReadOnly(True)
        self._output_view.setFont(_mono_font(9))
        self._output_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._output_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._output_view.setStyleSheet(
            f"background: {TERMINAL_BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px; "
            "font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
        )
        self._output_view.setFixedHeight(120)
        body_layout.addWidget(self._output_view)

        self._body.setVisible(True)  # Open by default for streaming
        layout.addWidget(self._body)

        self._refresh_header()

        # Throttle output updates to ~30fps to keep the GUI responsive
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flush)
        self._timer.setInterval(33)  # ~30 fps
        self._timer.start()

    def _toggle_body(self) -> None:
        self._body.setVisible(not self._body.isVisible())
        self._refresh_header()

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
        self._header.setText(f"{chev} > $ {self._command}  {state_str}")
        self._header.setStyleSheet(
            f"QToolButton#sectionToggle {{ color: {state_color}; }}"
        )

    def set_command(self, command: str) -> None:
        """Update the command shown in the header."""
        if command and command != "...":
            self._command = command
            self._refresh_header()

    def append_output(self, text: str) -> None:
        """Append a chunk of stdout/stderr text (buffered, flushed at 30fps)."""
        self._output_buf += text
        self._pending += text
        self._dirty = True

    def _flush(self) -> None:
        """Flush the pending output buffer to the QPlainTextEdit at most 30fps."""
        if not self._dirty:
            return
        self._dirty = False
        self._output_view.insertPlainText(self._pending)
        self._pending = ""
        # Auto-scroll to bottom
        sb = self._output_view.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._auto_size_output_view()

    def _auto_size_output_view(self) -> None:
        doc = self._output_view.document()
        doc_height = doc.size().height() + 12
        clamped = max(120, min(doc_height, 600))
        self._output_view.setFixedHeight(int(clamped))

    def set_result(self, exit_code: int) -> None:
        """Set the final state based on the exit code."""
        self._timer.stop()  # No more output coming
        self._state = self.STATE_DONE if exit_code == 0 else self.STATE_FAILED
        if exit_code != 0:
            # Auto-expand on failure
            self._body.setVisible(True)
        else:
            # Collapse on success (user can toggle to view)
            self._body.setVisible(False)
        self._refresh_header()
