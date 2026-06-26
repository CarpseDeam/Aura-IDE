"""Qt-backed coalescer for Worker Log prose stream fragments."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

from aura.gui.worker_log_stream.formatter import (
    compact_excess_blank_lines,
    needs_section_break,
    normalize_worker_log_text,
)


class WorkerLogStreamBuffer(QObject):
    """Batch tiny Worker prose deltas before inserting them into the log view."""

    def __init__(
        self,
        append_callback: Callable[[str], None],
        parent: QObject | None = None,
        interval_ms: int = 50,
    ) -> None:
        super().__init__(parent)
        self._append_callback = append_callback
        self._pending = ""
        self._tail = ""
        self._previous_kind: str | None = None
        self._boundary_pending = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.flush)

    @property
    def pending_text(self) -> str:
        """Return pending text, primarily for focused tests."""
        return self._pending

    @property
    def is_empty(self) -> bool:
        """Return True when there is no pending text waiting to flush."""
        return not self._pending

    def append(self, kind: str, text: str) -> None:
        """Queue one prose fragment and schedule a short coalesced flush."""
        normalized = normalize_worker_log_text(text)
        if not normalized:
            return

        current_tail = self._current_tail()
        if self._boundary_pending and current_tail.strip():
            self._pending += self._section_separator(current_tail)
            normalized = normalized.lstrip("\n")
            self._boundary_pending = False
        elif needs_section_break(current_tail, self._previous_kind, kind):
            self._pending += self._section_separator(current_tail)
            normalized = normalized.lstrip("\n")
        else:
            self._boundary_pending = False

        self._pending += normalized
        self._pending = compact_excess_blank_lines(self._pending)
        self._previous_kind = kind

        if not self._timer.isActive():
            self._timer.start()

    def flush(self) -> None:
        """Immediately emit all pending prose as one combined chunk."""
        if self._timer.isActive():
            self._timer.stop()
        if not self._pending:
            return

        text = self._pending
        self._pending = ""
        self._append_callback(text)
        self._remember_tail(text)

    def mark_boundary(self) -> None:
        """Ensure the next prose append starts on a clean paragraph boundary."""
        self.flush()
        self._boundary_pending = True

    def clear(self) -> None:
        """Drop pending prose and reset stream-kind tracking."""
        if self._timer.isActive():
            self._timer.stop()
        self._pending = ""
        self._tail = ""
        self._previous_kind = None
        self._boundary_pending = False

    def _current_tail(self) -> str:
        return (self._tail + self._pending)[-200:]

    def _remember_tail(self, text: str) -> None:
        self._tail = (self._tail + text)[-200:]

    @staticmethod
    def _section_separator(existing_text_tail: str) -> str:
        if existing_text_tail.endswith("\n\n"):
            return ""
        if existing_text_tail.endswith("\n"):
            return "\n"
        return "\n\n"
