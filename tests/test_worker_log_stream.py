from __future__ import annotations

from PySide6.QtWidgets import QApplication

from aura.gui.worker_log_stream import (
    WorkerLogStreamBuffer,
    compact_excess_blank_lines,
    needs_section_break,
    normalize_worker_log_text,
)


def test_normalize_worker_log_text_converts_crlf_to_lf() -> None:
    assert normalize_worker_log_text("a\r\nb\rc") == "a\nb\nc"


def test_compact_excess_blank_lines() -> None:
    assert compact_excess_blank_lines("a\n\n\n\nb") == "a\n\nb"


def test_needs_section_break_on_stream_kind_change() -> None:
    assert needs_section_break("changes", "reasoning", "content") is True
    assert needs_section_break("changes", "content", "content") is False
    assert needs_section_break("\n\n", "reasoning", "content") is False


def test_buffer_append_stores_pending_text() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "Hel")
    buffer.append("content", "lo")

    assert buffer.pending_text == "Hello"
    assert emitted == []


def test_buffer_flush_emits_one_combined_chunk() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "Hel")
    buffer.append("content", "lo")
    buffer.flush()

    assert emitted == ["Hello"]
    assert buffer.is_empty is True


def test_buffer_clear_drops_pending_text() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "stale")
    buffer.clear()
    buffer.flush()

    assert emitted == []
    assert buffer.is_empty is True


def test_buffer_kind_switch_separates_without_token_spacing() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("reasoning", "chan")
    buffer.append("reasoning", "ges")
    buffer.append("content", "Now let me")
    buffer.flush()

    assert emitted == ["changes\n\nNow let me"]


def test_buffer_mark_boundary_separates_same_kind_prose() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "changes")
    buffer.mark_boundary()
    buffer.append("content", "Now let me")
    buffer.flush()

    assert "".join(emitted) == "changes\n\nNow let me"


def test_buffer_mark_boundary_does_not_add_giant_blank_gaps() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "changes\n\n")
    buffer.mark_boundary()
    buffer.append("content", "Now let me")
    buffer.flush()

    assert "".join(emitted) == "changes\n\nNow let me"


def test_buffer_clear_resets_boundary_state() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "old")
    buffer.flush()
    buffer.mark_boundary()
    buffer.clear()
    buffer.append("content", "fresh")
    buffer.flush()

    assert emitted == ["old", "fresh"]


def _ensure_qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
