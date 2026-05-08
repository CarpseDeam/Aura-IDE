"""Embeddable panel for worker dispatch output.

Shows a pinned TODO list and one code-stream card per file edited by the worker,
with dark syntax highlighting.
"""

from __future__ import annotations

import json
import re

from PySide6.QtCore import QEasingCurve, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QScrollArea,
    QTextEdit,
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


class DarkSyntaxHighlighter(QSyntaxHighlighter):
    """Regex-based syntax highlighter using VS Code Dark+ theme colors."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rules: list[tuple[str, QTextCharFormat]] = []

        # Keywords — blue
        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor("#569CD6"))
        keywords = [
            "and", "as", "assert", "async", "await", "break", "case",
            "class", "class_name", "const", "continue", "def", "elif",
            "else", "enum", "except", "export", "extends", "false",
            "finally", "for", "from", "func", "global", "if", "import",
            "in", "is", "lambda", "match", "not", "null", "onready",
            "or", "pass", "print", "raise", "return", "self", "signal",
            "static", "super", "true", "try", "var", "while", "with",
            "yield",
        ]
        for kw in keywords:
            self._rules.append((rf"\b{kw}\b", kw_fmt))

        # Types / built-ins — teal
        type_fmt = QTextCharFormat()
        type_fmt.setForeground(QColor("#4EC9B0"))
        types = [
            "Array", "bool", "Color", "Dictionary", "float", "int",
            "Node", "Node2D", "Node3D", "Object", "PoolStringArray",
            "PoolIntArray", "PoolRealArray", "PackedByteArray",
            "PackedColorArray", "PackedFloat32Array", "PackedFloat64Array",
            "PackedInt32Array", "PackedInt64Array", "PackedStringArray",
            "PackedVector2Array", "PackedVector3Array", "Rect2", "Rect2i",
            "String", "Vector2", "Vector2i", "Vector3", "Vector3i",
            "Vector4", "Vector4i", "void",
        ]
        for t in types:
            self._rules.append((rf"\b{t}\b", type_fmt))

        # Numbers — light green
        num_fmt = QTextCharFormat()
        num_fmt.setForeground(QColor("#B5CEA8"))
        self._rules.append((r"\b\d+\.?\d*\b", num_fmt))
        self._rules.append((r"\b0x[0-9a-fA-F]+\b", num_fmt))

        # Strings (double-quoted) — orange
        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor("#CE9178"))
        self._rules.append((r'"[^"\\]*(\\.[^"\\]*)*"', str_fmt))

        # Strings (single-quoted) — orange
        self._rules.append((r"'[^'\\]*(\\.[^'\\]*)*'", str_fmt))

        # Comments (# style) — green
        cmt_fmt = QTextCharFormat()
        cmt_fmt.setForeground(QColor("#6A9955"))
        self._rules.append((r"#.*$", cmt_fmt))

        # Comments (// style) — green
        self._rules.append((r"//.*$", cmt_fmt))

        # Decorators / annotations — yellow
        dec_fmt = QTextCharFormat()
        dec_fmt.setForeground(QColor("#DCDCAA"))
        self._rules.append((r"@\w+", dec_fmt))

        # Compile all patterns into QRegularExpression objects for performance
        from PySide6.QtCore import QRegularExpression
        self._compiled: list[tuple[QRegularExpression, QTextCharFormat]] = []
        for pattern, fmt in self._rules:
            self._compiled.append((QRegularExpression(pattern), fmt))
        self._rules.clear()  # free the string list

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._compiled:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


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
            QFrame#codeStreamCard QTextEdit {{
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

        self._editor = QTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._highlighter = DarkSyntaxHighlighter(self._editor.document())
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
    """Shows a pinned TODO list and one code-stream card per file edited by the worker, with dark syntax highlighting."""

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

        # Scrollable container for multiple code-stream cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._card_container = QWidget()
        self._card_container.setStyleSheet("background: transparent;")
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(10)
        self._card_layout.addStretch(1)
        scroll.setWidget(self._card_container)
        self._cards: list[CodeStreamCard] = []
        self._current_card: CodeStreamCard | None = None
        self._scroll = scroll

        layout.addWidget(scroll, 1)

        self._write_tools: dict[str, dict] = {}  # worker_tool_id -> {name, buffered_args, last_content_len, path}

    # ---- helpers -----------------------------------------------------------

    def _new_card(self) -> CodeStreamCard:
        """Finish the current card (if any), create and return a new one."""
        if self._current_card is not None:
            self._current_card.finish()
        card = CodeStreamCard()
        self._cards.append(card)
        # Insert before the trailing stretch
        self._card_layout.insertWidget(self._card_layout.count() - 1, card)
        self._current_card = card
        card.begin()
        return card

    def _scroll_to_bottom(self) -> None:
        """Scroll the outer scroll area to the bottom."""
        bar = self._scroll.verticalScrollBar()
        if bar:
            bar.setValue(bar.maximum())

    # ---- public streaming API ----------------------------------------------

    def begin_assistant(self) -> None:
        # Remove all existing cards
        for card in self._cards:
            card.finish()
            card.deleteLater()
        self._cards.clear()
        self._current_card = None
        self._write_tools.clear()

    def append_reasoning(self, text: str) -> None:
        """Drop all thinking — no-op."""

    def append_content(self, text: str) -> None:
        """Drop all content text — no-op."""

    def add_tool_call(self, worker_tool_id: str, name: str) -> None:
        """Only track write_file and edit_file tool calls."""
        if name in ("write_file", "edit_file"):
            self._write_tools[worker_tool_id] = {
                "name": name,
                "buffered_args": "",
                "last_content_len": 0,
                "path": "",
            }

    def append_tool_args(self, worker_tool_id: str, fragment: str) -> None:
        """Extract code content from streaming JSON and feed new characters to the card."""
        info = self._write_tools.get(worker_tool_id)
        if info is None:
            return

        info["buffered_args"] += fragment

        try:
            parsed = json.loads(info["buffered_args"])
        except json.JSONDecodeError:
            # Try regex to extract path from partial JSON for the header
            m = re.search(r'"path"\s*:\s*"([^"]*)', info["buffered_args"])
            if m and not info["path"]:
                info["path"] = m.group(1)
                if self._current_card is None:
                    self._new_card()
                self._current_card.append(f"📄 {info['path']}\n\n")
                self._scroll_to_bottom()
            return

        # Successfully parsed full JSON
        path = parsed.get("path", "")
        if path and path != info["path"]:
            info["path"] = path
            if self._current_card is None:
                self._new_card()
            self._current_card.append(f"📄 {path}\n\n")

        content_key = "content" if info["name"] == "write_file" else "new_str"
        content = parsed.get(content_key, "")
        new_chars = content[info["last_content_len"] :]
        if new_chars:
            if self._current_card is None:
                self._new_card()
            self._current_card.append(new_chars)
            info["last_content_len"] = len(content)
            self._scroll_to_bottom()

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str) -> None:
        """On failure append a brief failure marker; on success, nothing extra."""
        if worker_tool_id in self._write_tools:
            del self._write_tools[worker_tool_id]
            if not ok and self._current_card is not None:
                self._current_card.append("\n// ✗ failed\n")
                self._scroll_to_bottom()

    def append_terminal_output(self, worker_tool_id: str, text: str) -> None:
        """Drop all terminal output — no-op."""

    def add_diff_card(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        """Drop all diff cards — no-op."""

    def add_error(self, message: str) -> None:
        """Drop all errors — no-op."""

    def worker_finished(self, ok: bool, summary: str) -> None:
        if self._current_card is not None:
            self._current_card.finish()

    def worker_cancelled(self) -> None:
        if self._current_card is not None:
            self._current_card.finish()

    def update_todo_list(self, tasks: list) -> None:
        """Forward the worker's TODO list update to the pinned widget."""
        self._todo_widget.update_tasks(tasks)

    def clear(self) -> None:
        """Remove all card content and reset state (called on New Conversation)."""
        for card in self._cards:
            card.clear()
            card.deleteLater()
        self._cards.clear()
        self._current_card = None
        self._todo_widget.update_tasks([])
        self._write_tools.clear()
