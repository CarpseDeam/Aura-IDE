"""Embeddable panel for worker dispatch output.

Shows a pinned TODO list and a single scrolling card for worker streaming output.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import BG, BORDER, FG, FG_DIM, SUCCESS, WARN


class TodoListWidget(QFrame):
    """Pinned TODO list showing the worker's execution plan with live status updates."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("todoListWidget")
        self.setStyleSheet(
            f"QFrame#todoListWidget {{"
            f"  background: {BG};"
            f"  border-bottom: 1px solid {BORDER};"
            f"  padding: 0;"
            f"}}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(4)

        # Header
        header = QLabel("TODO LIST")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 0 0 4px 0;")
        outer.addWidget(header)

        # Container for task labels
        self._tasks_layout = QVBoxLayout()
        self._tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_layout.setSpacing(2)
        outer.addLayout(self._tasks_layout)

        self._pulse_anims: list[QVariantAnimation] = []

        self.setVisible(False)  # Hidden until tasks arrive

    def update_tasks(self, tasks: list[dict]) -> None:
        """Clear and redraw the task list from the worker's update_todo_list tool."""
        # Stop any running pulse animations
        for anim in self._pulse_anims:
            anim.stop()
            anim.deleteLater()
        self._pulse_anims.clear()

        # Remove old task labels
        while self._tasks_layout.count() > 0:
            item = self._tasks_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        if not tasks:
            self.setVisible(False)
            return

        self.setVisible(True)

        for task in tasks:
            description = task.get("description", "")
            status = task.get("status", "pending")

            # Choose prefix and color
            if status == "done":
                prefix = "✓"
                color = SUCCESS
            elif status == "active":
                prefix = "►"
                color = WARN
            else:  # pending
                prefix = "○"
                color = FG_DIM

            label_text = f"{prefix} {description}"
            label = QLabel(label_text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

            # Monospace font
            font = label.font()
            font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setPointSize(11)
            label.setFont(font)

            # Bold for active tasks
            if status == "active":
                font.setBold(True)
                label.setFont(font)

                # Add a breathing pulse animation to the label
                effect = QGraphicsOpacityEffect(label)
                effect.setOpacity(1.0)
                label.setGraphicsEffect(effect)

                pulse = QVariantAnimation(label)
                pulse.setStartValue(0.55)
                pulse.setEndValue(1.0)
                pulse.setDuration(900)
                pulse.setLoopCount(-1)
                pulse.setEasingCurve(QEasingCurve.Type.InOutSine)

                def _make_opacity_setter(eff):
                    return lambda v: eff.setOpacity(v)

                pulse.valueChanged.connect(_make_opacity_setter(effect))
                pulse.start()
                self._pulse_anims.append(pulse)

            label.setStyleSheet(f"color: {color}; padding: 1px 0;")
            self._tasks_layout.addWidget(label)


class CodeStreamCard(QFrame):
    """Dark glass card with a character-by-character typing animation.

    Renders incoming text progressively via a flush timer (3 chars every 20ms,
    ~150 chars/sec) and shows a blinking block cursor ``▌`` at the end of the
    document while the card is active.
    """

    CURSOR_CHAR = "▌"  # U+258C LEFT HALF BLOCK
    FLUSH_INTERVAL = 20       # ms (~50 fps)
    CHARS_PER_TICK = 3        # ~150 chars/sec
    BLINK_INTERVAL = 530      # ms

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("codeStreamCard")
        self.setStyleSheet(f"""
            QFrame#codeStreamCard {{
                background: rgba(28, 28, 34, 0.50);
                border-top: 1px solid rgba(255, 255, 255, 0.06);
                border-right: 1px solid rgba(0, 0, 0, 0.18);
                border-bottom: 1px solid rgba(0, 0, 0, 0.25);
                border-left: 1px solid rgba(255, 255, 255, 0.04);
                border-radius: 10px;
            }}
            QFrame#codeStreamCard QPlainTextEdit {{
                background: transparent;
                color: {FG};
                border: none;
                padding: 12px;
                font-family: "Geist Mono", "JetBrains Mono", "Consolas", monospace;
                font-size: 11pt;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._editor = QPlainTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self._editor)

        # Internal state
        self._buffer = ""
        self._active = False
        self._cursor_visible = False

        # Flush timer – releases characters from the buffer
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(self.FLUSH_INTERVAL)
        self._flush_timer.timeout.connect(self._flush)

        # Blink timer – toggles the trailing block cursor
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(self.BLINK_INTERVAL)
        self._blink_timer.timeout.connect(self._blink)

        self.setVisible(False)

    # --- Public API ---------------------------------------------------------

    def append(self, text: str) -> None:
        """Add *text* to the typing buffer.

        The text will be progressively revealed by the flush timer.  Has no
        effect when the card is not active.
        """
        if not self._active:
            return
        self._buffer += text
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def begin(self) -> None:
        """Clear the editor, show the card, and start the typing effect."""
        self._editor.clear()
        self._buffer = ""
        self._active = True
        self.setVisible(True)
        self._append_cursor()
        self._flush_timer.start()
        self._blink_timer.start()

    def finish(self) -> None:
        """Flush any remaining buffered text, remove the cursor, and stop timers."""
        if not self._active:
            return
        self._active = False
        if self._buffer:
            self._flush_buffer(self._buffer)
            self._buffer = ""
        self._remove_cursor()
        self._flush_timer.stop()
        self._blink_timer.stop()

    def clear(self) -> None:
        """Immediately clear all content, hide the card, and stop all timers."""
        self._active = False
        self._buffer = ""
        self._flush_timer.stop()
        self._blink_timer.stop()
        self._editor.clear()
        self.setVisible(False)

    # --- Internals ----------------------------------------------------------

    def _flush(self) -> None:
        """Release up to *CHARS_PER_TICK* characters from the buffer."""
        if not self._active:
            return
        if self._buffer:
            chunk = self._buffer[: self.CHARS_PER_TICK]
            self._buffer = self._buffer[self.CHARS_PER_TICK :]
            self._flush_buffer(chunk)

    def _flush_buffer(self, text: str) -> None:
        """Insert *text* into the editor while preserving the trailing cursor."""
        self._remove_cursor()
        self._editor.insertPlainText(text)
        self._append_cursor()
        self._scroll_to_end()

    def _blink(self) -> None:
        """Toggle the ``▌`` cursor at the end of the document."""
        if not self._active:
            return
        if self._cursor_visible:
            self._remove_cursor()
        else:
            self._append_cursor()

    def _remove_cursor(self) -> None:
        """Remove the trailing cursor character if it is present."""
        doc = self._editor.document()
        text = doc.toPlainText()
        if text.endswith(self.CURSOR_CHAR):
            cursor = self._editor.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.movePosition(
                cursor.MoveOperation.Left, cursor.MoveMode.KeepAnchor, 1
            )
            cursor.removeSelectedText()
            self._cursor_visible = False

    def _append_cursor(self) -> None:
        """Append the cursor character at the very end of the document."""
        cursor = self._editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(self.CURSOR_CHAR)
        self._cursor_visible = True

    def _scroll_to_end(self) -> None:
        """Scroll the editor to the bottom."""
        scrollbar = self._editor.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())


class WorkerWindow(QWidget):
    """Embeddable panel showing live worker activity for all dispatches."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QLabel("Worker")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 8px 12px;")
        layout.addWidget(header)

        # Pinned TODO list
        self._todo_widget = TodoListWidget()
        layout.addWidget(self._todo_widget)

        # Single scrollable card for worker output
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._card = CodeStreamCard()
        self._card.setVisible(False)

        scroll.setWidget(self._card)
        layout.addWidget(scroll, 1)

    # ---- public streaming API ----------------------------------------------

    def begin_assistant(self) -> None:
        self._card.begin()

    def append_reasoning(self, text: str) -> None:
        self._card.append(f"/* thinking */ {text}")

    def append_content(self, text: str) -> None:
        self._card.append(text)

    def add_tool_call(self, worker_tool_id: str, name: str) -> None:
        if name == "update_todo_list":
            return  # Pinned todo widget handles this, don't render a tool card.
        self._card.append(f"\n// 🛠 {name}\n")

    def append_tool_args(self, worker_tool_id: str, fragment: str) -> None:
        # no-op: simplified card doesn't show tool args
        pass

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str) -> None:
        self._card.append(" // ✓\n" if ok else " // ✗\n")

    def append_terminal_output(self, worker_tool_id: str, text: str) -> None:
        self._card.append(text)

    def add_diff_card(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        self._card.append(f"\n// 📄 {decision}: {rel_path}\n")

    def add_error(self, message: str) -> None:
        self._card.append(f"\n// ❌ {message}\n")

    def worker_finished(self, ok: bool, summary: str) -> None:
        self._card.append(
            f"\n// {'✅ Completed' if ok else '❌ Completed with errors'}\n"
        )
        self._card.finish()

    def worker_cancelled(self) -> None:
        self._card.append("\n// ⏹ Cancelled\n")
        self._card.finish()

    def update_todo_list(self, tasks: list) -> None:
        """Forward the worker's TODO list update to the pinned widget."""
        self._todo_widget.update_tasks(tasks)

    def clear(self) -> None:
        """Remove all card content and reset state (called on New Conversation)."""
        self._card.clear()
        self._todo_widget.update_tasks([])
