"""Floating terminal transcript window for shell and CLI-agent streams.

This window is a read-only observer. Everything Aura runs — shell commands and
CLI-agent processes alike — is appended chronologically to one continuous
transcript, the way a conventional coding-agent console reads::

    C:/Projects/Aura-Harness2
    ❯ pytest -q
    ........
    ✓ exited 0

Tool and process ids are kept only to correlate streaming events back to the
transcript; no widget is ever created per id.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QElapsedTimer, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards._helpers import _mono_font
from aura.gui.scrollbar_style import aura_scrollbar_qss
from aura.gui.theme import ACCENT, BG, BORDER, DANGER, FG, FG_DIM, SUCCESS, TERMINAL_BG

PROMPT_GLYPH = "❯"
OK_GLYPH = "✓"
FAIL_GLYPH = "✗"


class TerminalWindow(QDialog):
    """Non-modal floating terminal transcript window.

    The window hides instead of being destroyed, so output continues buffering
    while hidden and reappears when the edge tab opens it again.
    """

    terminal_started = Signal()
    terminal_finished = Signal(object)
    visibility_changed = Signal(bool)
    terminal_cleared = Signal()
    geometry_saved = Signal(str)

    #: One global cap on transcript growth, shared by every command.
    MAX_BLOCKS = 5000
    #: Minimum gap between document writes, so rapid streaming stays responsive.
    FLUSH_INTERVAL_MS = 33
    #: How close to the bottom still counts as "following" new output.
    FOLLOW_SLACK_PX = 24

    def __init__(
        self,
        parent: QWidget | None = None,
        initial_geometry: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Terminal")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self._geometry_restore_done = False
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(250)
        self._geometry_save_timer.timeout.connect(self._save_geometry)
        self.resize(860, 460)

        self._initial_geometry = initial_geometry.strip()

        # Ids of commands started but not yet finished. Event correlation
        # only — an id never owns a widget, and it is dropped again once its
        # exit status lands so a later command may reuse it.
        self._active_ids: set[str] = set()

        # One ordered, globally throttled buffer of (style, text) fragments.
        self._pending: list[tuple[str, str]] = []
        self._current_cwd = ""
        self._has_content = False
        self._at_line_start = True
        self._auto_follow = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("terminalWindowHeader")
        header.setFixedHeight(36)
        header.setStyleSheet(
            f"QFrame#terminalWindowHeader {{"
            f"  background: {BG};"
            f"  border-bottom: 1px solid {BORDER};"
            f"}}"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 8, 0)
        header_layout.setSpacing(8)

        title = QLabel("Terminal", header)
        title.setStyleSheet(f"color: {FG}; font-weight: 600;")
        header_layout.addWidget(title)
        header_layout.addStretch(1)

        subtle_button_qss = (
            f"QToolButton {{"
            f"  background: transparent;"
            f"  color: {FG_DIM};"
            f"  border: none;"
            f"  padding: 2px 8px;"
            f"}}"
            f"QToolButton:hover {{ color: {FG}; }}"
        )

        self._clear_btn = QToolButton(header)
        self._clear_btn.setText("Clear")
        self._clear_btn.setToolTip("Clear the terminal transcript")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setStyleSheet(subtle_button_qss)
        self._clear_btn.clicked.connect(self.clear_display)
        header_layout.addWidget(self._clear_btn)

        close_btn = QToolButton(header)
        close_btn.setText("x")
        close_btn.setToolTip("Hide terminal")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            subtle_button_qss + "QToolButton { font-size: 14px; }"
        )
        close_btn.clicked.connect(self.hide)
        header_layout.addWidget(close_btn)
        outer.addWidget(header)

        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setFont(_mono_font(10))
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._view.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  background: {TERMINAL_BG};"
            f"  color: {FG};"
            f"  border: none;"
            f"  padding: 10px 12px;"
            f"  selection-background-color: {ACCENT};"
            f"  selection-color: {BG};"
            f"}}"
            + aura_scrollbar_qss("QPlainTextEdit")
        )
        self._view.document().setMaximumBlockCount(self.MAX_BLOCKS)
        self._view.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        outer.addWidget(self._view, 1)

        self._formats = {
            "cwd": self._make_format(FG_DIM),
            "command": self._make_format(ACCENT),
            "output": self._make_format(FG),
            "ok": self._make_format(SUCCESS),
            "fail": self._make_format(DANGER),
        }

        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush)
        self._since_flush = QElapsedTimer()
        self._since_flush.start()

        self.setStyleSheet(f"QDialog {{ background: {TERMINAL_BG}; color: {FG}; }}")
        self._restore_geometry(self._initial_geometry)
        self._geometry_restore_done = True

    # ------------------------------------------------------------------
    # Transcript API
    # ------------------------------------------------------------------

    def set_command(self, tool_id: str, command: str, cwd: str = "") -> None:
        """Append one command to the transcript, keyed by its tool/process id."""
        if tool_id in self._active_ids:
            return
        self._active_ids.add(tool_id)

        if self._has_content:
            self._queue_line_break()
            self._queue("output", "\n")
        cwd = (cwd or "").strip()
        if cwd and cwd != self._current_cwd:
            self._current_cwd = cwd
            self._queue("cwd", f"{cwd}\n")
        self._queue("command", f"{PROMPT_GLYPH} {command or ''}\n")
        self._has_content = True
        self.terminal_started.emit()

    def append_output(self, tool_id: str, text: str) -> None:
        """Append streamed output for an active id, even while hidden."""
        if not text or tool_id not in self._active_ids:
            return
        self._queue("output", text)

    def set_result(self, tool_id: str, exit_code: int) -> None:
        """Append the compact exit-status line without changing visibility.

        Finishing retires the id, so a duplicate or orphan result is ignored
        and the same id is free to key a later command.
        """
        if tool_id not in self._active_ids:
            return
        self._active_ids.discard(tool_id)
        self._queue_line_break()
        if exit_code == 0:
            self._queue("ok", f"{OK_GLYPH} exited 0\n")
        else:
            self._queue("fail", f"{FAIL_GLYPH} exited {exit_code}\n")
        self.terminal_finished.emit(exit_code)

    @property
    def has_active_commands(self) -> bool:
        """Whether any started command is still awaiting its exit status."""
        return bool(self._active_ids)

    def clear_display(self) -> None:
        """Empty what is rendered without abandoning commands still running.

        The header's Clear button lands here. Only presentation state is
        reset, so output and the final exit status of anything still active
        keep arriving in the fresh transcript.
        """
        self._clear_rendered()
        self.terminal_cleared.emit()

    def reset(self) -> None:
        """Drop the transcript and every active id at a full reset boundary.

        The abandoned execution's late output stays ignored afterwards,
        because its id is no longer active.
        """
        self._clear_rendered()
        self._active_ids.clear()
        self.terminal_cleared.emit()

    def _clear_rendered(self) -> None:
        """Empty the document, the pending buffer and presentation state."""
        self._flush_timer.stop()
        self._pending.clear()
        self._view.clear()
        self._current_cwd = ""
        self._has_content = False
        self._at_line_start = True
        self._auto_follow = True

    def transcript_text(self) -> str:
        """Return the rendered transcript, flushing anything still buffered."""
        self._flush()
        return self._view.toPlainText()

    # ------------------------------------------------------------------
    # Ordered, throttled buffering
    # ------------------------------------------------------------------

    def _queue(self, style: str, text: str) -> None:
        self._pending.append((style, text))
        self._at_line_start = text.endswith("\n")
        self._schedule_flush()

    def _queue_line_break(self) -> None:
        """Ensure the next fragment starts on a fresh line."""
        if not self._at_line_start:
            self._queue("output", "\n")

    def _schedule_flush(self) -> None:
        if self._flush_timer.isActive():
            return
        elapsed = self._since_flush.elapsed()
        if elapsed >= self.FLUSH_INTERVAL_MS:
            self._flush()
        else:
            self._flush_timer.start(self.FLUSH_INTERVAL_MS - int(elapsed))

    def _flush(self) -> None:
        """Write every buffered fragment to the document in event order."""
        self._flush_timer.stop()
        self._since_flush.restart()
        if not self._pending:
            return
        pending, self._pending = self._pending, []

        # A detached cursor leaves the viewport wherever the user left it.
        cursor = QTextCursor(self._view.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        for style, text in pending:
            cursor.insertText(text, self._formats[style])
        cursor.endEditBlock()

        if self._auto_follow:
            scrollbar = self._view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _on_scrolled(self, value: int) -> None:
        """Follow new output only while the user is at or near the bottom."""
        scrollbar = self._view.verticalScrollBar()
        self._auto_follow = value >= scrollbar.maximum() - self.FOLLOW_SLACK_PX

    @staticmethod
    def _make_format(color: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        return fmt

    # ------------------------------------------------------------------
    # Window behaviour
    # ------------------------------------------------------------------

    def show_and_raise(self) -> None:
        """Show this floating window and bring it to the front."""
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle(self) -> None:
        """Toggle between visible and hidden."""
        if self.isVisible():
            self.hide()
        else:
            self.show_and_raise()

    def is_open(self) -> bool:
        """Return whether the floating terminal window is visible."""
        return self.isVisible()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._schedule_geometry_save()
        self.visibility_changed.emit(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._flush()
        self.visibility_changed.emit(True)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._schedule_geometry_save()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self._save_geometry()
        self.hide()

    def _restore_geometry(self, geometry: str) -> None:
        if not geometry:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        except Exception:
            return

    def _schedule_geometry_save(self) -> None:
        if not self._geometry_restore_done:
            return
        self._geometry_save_timer.start()

    def _save_geometry(self) -> None:
        if not self._geometry_restore_done:
            return
        geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.geometry_saved.emit(geometry)
