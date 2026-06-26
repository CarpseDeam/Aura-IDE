"""Pure text normalization helpers for Worker Log prose streams."""

from __future__ import annotations

import re


_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def normalize_worker_log_text(text: str) -> str:
    """Normalize platform newlines without changing streamed word boundaries."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def compact_excess_blank_lines(text: str) -> str:
    """Limit prose spacing to one blank line between sections."""
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", text)


def needs_section_break(
    existing_text_tail: str,
    previous_kind: str | None,
    next_kind: str,
) -> bool:
    """Return whether a stream transition needs visible paragraph separation."""
    if previous_kind is None or previous_kind == next_kind:
        return False
    if not existing_text_tail or not existing_text_tail.strip():
        return False
    return not existing_text_tail.endswith("\n\n")
