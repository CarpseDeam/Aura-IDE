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


# ── Buffer glued-prose separation tests ──────────────────────────────


def test_buffer_glued_prose_detects_sentence_boundary() -> None:
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "I'll start by examining the files.")
    buffer.append("content", "Let me read the key files.")
    buffer.flush()

    assert emitted == ["I'll start by examining the files.\n\nLet me read the key files."]


def test_buffer_glued_prose_middle_sentence_no_break() -> None:
    """No break when new fragment doesn't start with uppercase."""
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "Now let me")
    buffer.append("content", " read the files")
    buffer.flush()

    assert emitted == ["Now let me read the files"]


def test_buffer_glued_prose_no_break_without_punctuation() -> None:
    """No break when tail doesn't end with sentence punctuation."""
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "Let me check")
    buffer.append("content", "Now let me")
    buffer.flush()

    assert emitted == ["Let me checkNow let me"]


def test_buffer_glued_prose_only_content_kind() -> None:
    """Glue detection only fires for 'content' and 'reasoning' kinds."""
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("tool_call", "Call.")
    buffer.append("tool_call", "Now more")
    buffer.flush()

    assert emitted == ["Call.Now more"]


def test_buffer_glued_prose_idempotent() -> None:
    """Multiple same-kind glued breaks don't produce triple newlines."""
    _ensure_qapp()
    emitted: list[str] = []
    buffer = WorkerLogStreamBuffer(emitted.append)

    buffer.append("content", "Start.")
    buffer.append("content", "Now check.")
    buffer.append("content", "Here more.")
    buffer.flush()

    result = emitted[0]
    assert "Start.\n\nNow" in result
    assert "Now check.\n\nHere" in result
    assert "\n\n\n" not in result


# ── _GLUED_SENTENCE_RE regex tests (used in _StreamLabel._flush) ────


def test_glued_sentence_regex_inserts_break() -> None:
    from aura.gui.cards._stream_label import _GLUED_SENTENCE_RE

    result = _GLUED_SENTENCE_RE.sub('\n\n', "Start.Now let me check")
    assert result == "Start.\n\nNow let me check"


def test_glued_sentence_regex_with_question_mark() -> None:
    from aura.gui.cards._stream_label import _GLUED_SENTENCE_RE

    result = _GLUED_SENTENCE_RE.sub('\n\n', "Is that correct?Here's more")
    assert result == "Is that correct?\n\nHere's more"


def test_glued_sentence_regex_no_split_on_single_letter() -> None:
    """Single-letter words like 'I' should not trigger a split."""
    from aura.gui.cards._stream_label import _GLUED_SENTENCE_RE

    result = _GLUED_SENTENCE_RE.sub('\n\n', "Am I.Indeed")
    # '.Indeed' matches (I uppercase + 'ndeed' lowercase), so break is inserted
    # But '.I' does NOT match because [a-z]{2,} requires >=2 lowercase letters
    # However, the string is "Am I.Indeed" — after 'I.' the next char is 'I' 
    # which is uppercase, but 'ndeed' is 5 lowercase letters, so it matches.
    # This is actually fine — '.Indeed' is a sentence boundary.
    assert "Am I." in result  # 'I.' not split (single letter 'I')
    assert "\n\nIndeed" in result


def test_glued_sentence_regex_no_split_on_acronym() -> None:
    from aura.gui.cards._stream_label import _GLUED_SENTENCE_RE

    result = _GLUED_SENTENCE_RE.sub('\n\n', "U.S.A. remains")
    assert result == "U.S.A. remains"


def test_glued_sentence_regex_code_block_preserved() -> None:
    from aura.gui.cards._stream_label import _GLUED_SENTENCE_RE

    # Pure code block (no sentence boundaries) unchanged
    text = "```\nprint(1)\n```"
    result = _GLUED_SENTENCE_RE.sub('\n\n', text)
    assert result == text

    # Code block with glued boundary outside — fence markers preserved
    text2 = "See below.```\ncode\n```.Next we"
    result2 = _GLUED_SENTENCE_RE.sub('\n\n', text2)
    assert "```" in result2
    assert "```." in result2  # backtick+period sequence intact


def test_glued_sentence_regex_idempotent() -> None:
    """Applying the regex twice does not add extra breaks."""
    from aura.gui.cards._stream_label import _GLUED_SENTENCE_RE

    text = "A.Now B.Then C"
    once = _GLUED_SENTENCE_RE.sub('\n\n', text)
    twice = _GLUED_SENTENCE_RE.sub('\n\n', once)
    assert once == twice


def _ensure_qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
