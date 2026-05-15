"""Smooth local text reveal for code editors."""

from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit


STREAM_TICK_MS = 16
NORMAL_CHARS_PER_SECOND = 1200
CATCHUP_CHARS_PER_SECOND = 3500
MIN_CHARS_PER_FRAME = 2
MAX_CHARS_PER_FRAME = 80
CATCHUP_DISTANCE = 1500
IMMEDIATE_FINISH_DISTANCE = 100_000
BOTTOM_THRESHOLD_PX = 64


class SmoothCodeStreamer(QObject):
    """Reveal text in a ``QPlainTextEdit`` without replacing the document per tick."""

    text_changed = Signal()
    finished = Signal()

    def __init__(self, editor: QPlainTextEdit, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._editor = editor
        self._timer = QTimer(self)
        self._timer.setInterval(STREAM_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._elapsed = QElapsedTimer()
        self._last_elapsed_ms = 0

        self._mode = "append"
        self._target_text = ""
        self._visible_text = ""
        self._replace_prefix = ""
        self._replace_suffix = ""
        self._target_insert_text = ""
        self._visible_insert_len = 0
        self._finishing = False
        self._programmatic_scroll_depth = 0
        self._auto_follow_bottom = True

        self._editor.viewport().installEventFilter(self)
        self._editor.verticalScrollBar().installEventFilter(self)
        self._editor.verticalScrollBar().valueChanged.connect(
            self._on_scroll_value_changed
        )

    def eventFilter(self, watched, event) -> bool:
        try:
            watched_editor_part = watched in (
                self._editor.viewport(),
                self._editor.verticalScrollBar(),
            )
        except RuntimeError:
            return False
        if watched_editor_part:
            if event.type() in (
                QEvent.Type.Wheel,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.KeyPress,
            ):
                self._auto_follow_bottom = self._is_near_bottom()
        return super().eventFilter(watched, event)

    def set_target(self, text: str) -> None:
        """Set the latest full target text and reveal toward it smoothly."""
        if self._mode == "replace" and self._can_update_replacement(text):
            self._target_text = text
            self._target_insert_text = self._replacement_middle(text)
            self._start_timer()
            return

        if not text.startswith(self._visible_text):
            self._replace_all_text("")
            self._visible_text = ""

        self._mode = "append"
        self._target_text = text
        self._replace_prefix = ""
        self._replace_suffix = ""
        self._target_insert_text = ""
        self._visible_insert_len = 0
        self._start_timer()

    def start_replacement(
        self,
        prefix: str,
        inserted_text: str,
        suffix: str,
        *,
        base_already_set: bool = False,
    ) -> None:
        """Reveal ``inserted_text`` between stable ``prefix`` and ``suffix``."""
        self.stop()
        self._mode = "replace"
        self._replace_prefix = prefix
        self._replace_suffix = suffix
        self._target_insert_text = inserted_text
        self._visible_insert_len = 0
        self._target_text = prefix + inserted_text + suffix
        self._visible_text = prefix + suffix
        self._finishing = False
        if not base_already_set:
            self._replace_all_text(self._visible_text)
        self._set_cursor_position(len(prefix))
        self._start_timer()

    def set_text_immediately(self, text: str) -> None:
        """Replace the editor document immediately and reset stream state."""
        self.stop()
        self._mode = "append"
        self._target_text = text
        self._visible_text = text
        self._replace_prefix = ""
        self._replace_suffix = ""
        self._target_insert_text = ""
        self._visible_insert_len = 0
        self._finishing = False
        self._replace_all_text(text)
        self._set_cursor_position(len(text))
        self.text_changed.emit()

    def finish(self, *, immediate: bool = False) -> None:
        """Finish revealing the current target, using catch-up speed by default."""
        remaining = self._remaining_chars()
        if remaining <= 0:
            self._timer.stop()
            self.finished.emit()
            return
        if immediate or remaining > IMMEDIATE_FINISH_DISTANCE:
            self.set_text_immediately(self._target_text)
            self.finished.emit()
            return
        self._finishing = True
        self._start_timer()

    def stop(self) -> None:
        self._timer.stop()
        self._finishing = False

    def is_active(self) -> bool:
        return self._timer.isActive()

    def visible_text(self) -> str:
        return self._visible_text

    def target_text(self) -> str:
        return self._target_text

    def _can_update_replacement(self, text: str) -> bool:
        if not text.startswith(self._replace_prefix) or not text.endswith(
            self._replace_suffix
        ):
            return False
        middle = self._replacement_middle(text)
        visible_middle = self._target_insert_text[:self._visible_insert_len]
        return middle.startswith(visible_middle)

    def _replacement_middle(self, text: str) -> str:
        suffix_len = len(self._replace_suffix)
        end = len(text) - suffix_len if suffix_len else len(text)
        return text[len(self._replace_prefix):end]

    def _start_timer(self) -> None:
        if self._remaining_chars() <= 0:
            self._timer.stop()
            self.finished.emit()
            return
        if not self._elapsed.isValid():
            self._elapsed.start()
            self._last_elapsed_ms = 0
        elif not self._timer.isActive():
            self._last_elapsed_ms = self._elapsed.elapsed()
        self._timer.start()

    def _tick(self, elapsed_ms: int | None = None) -> None:
        if elapsed_ms is None:
            now = self._elapsed.elapsed() if self._elapsed.isValid() else 0
            elapsed_ms = max(1, now - self._last_elapsed_ms)
            self._last_elapsed_ms = now

        remaining = self._remaining_chars()
        if remaining <= 0:
            self._timer.stop()
            self.finished.emit()
            return

        rate = (
            CATCHUP_CHARS_PER_SECOND
            if self._finishing or remaining > CATCHUP_DISTANCE
            else NORMAL_CHARS_PER_SECOND
        )
        char_count = int(rate * elapsed_ms / 1000)
        char_count = max(MIN_CHARS_PER_FRAME, min(MAX_CHARS_PER_FRAME, char_count))
        char_count = min(char_count, remaining)

        if self._mode == "replace":
            self._append_replacement_chars(char_count)
        else:
            self._append_chars(char_count)

        self.text_changed.emit()
        if self._remaining_chars() <= 0:
            self._timer.stop()
            self._finishing = False
            self.finished.emit()

    def _append_chars(self, char_count: int) -> None:
        start = len(self._visible_text)
        end = start + char_count
        delta = self._target_text[start:end]
        if not delta:
            return
        self._insert_text(start, delta)
        self._visible_text = self._target_text[:end]

    def _append_replacement_chars(self, char_count: int) -> None:
        start = self._visible_insert_len
        end = start + char_count
        delta = self._target_insert_text[start:end]
        if not delta:
            return
        insert_at = len(self._replace_prefix) + start
        self._insert_text(insert_at, delta)
        self._visible_insert_len = end
        self._visible_text = (
            self._replace_prefix
            + self._target_insert_text[:end]
            + self._replace_suffix
        )

    def _insert_text(self, position: int, text: str) -> None:
        bar = self._editor.verticalScrollBar()
        old_scroll = bar.value()
        cursor = QTextCursor(self._editor.document())
        cursor.setPosition(max(0, min(position, self._editor.document().characterCount())))
        cursor.insertText(text)
        if self._auto_follow_bottom:
            self._set_cursor_position(position + len(text))
            self._set_scrollbar_to_bottom()
        else:
            self._set_scrollbar_value(old_scroll)

    def _replace_all_text(self, text: str) -> None:
        bar = self._editor.verticalScrollBar()
        old_scroll = bar.value()
        self._programmatic_scroll_depth += 1
        self._editor.setPlainText(text)
        self._programmatic_scroll_depth = max(0, self._programmatic_scroll_depth - 1)
        if self._auto_follow_bottom:
            self._set_scrollbar_to_bottom()
        else:
            self._set_scrollbar_value(old_scroll)

    def _set_cursor_position(self, position: int) -> None:
        cursor = QTextCursor(self._editor.document())
        max_pos = max(0, self._editor.document().characterCount() - 1)
        cursor.setPosition(max(0, min(position, max_pos)))
        self._editor.setTextCursor(cursor)

    def _remaining_chars(self) -> int:
        if self._mode == "replace":
            return max(0, len(self._target_insert_text) - self._visible_insert_len)
        return max(0, len(self._target_text) - len(self._visible_text))

    def _is_near_bottom(self) -> bool:
        bar = self._editor.verticalScrollBar()
        return bar.maximum() - bar.value() <= BOTTOM_THRESHOLD_PX

    def _on_scroll_value_changed(self, value: int) -> None:
        if self._programmatic_scroll_depth > 0:
            return
        bar = self._editor.verticalScrollBar()
        self._auto_follow_bottom = bar.maximum() - value <= BOTTOM_THRESHOLD_PX

    def _set_scrollbar_to_bottom(self) -> None:
        bar = self._editor.verticalScrollBar()
        self._set_scrollbar_value(bar.maximum())

    def _set_scrollbar_value(self, value: int) -> None:
        bar = self._editor.verticalScrollBar()
        self._programmatic_scroll_depth += 1
        bar.setValue(max(bar.minimum(), min(value, bar.maximum())))
        self._programmatic_scroll_depth = max(0, self._programmatic_scroll_depth - 1)
