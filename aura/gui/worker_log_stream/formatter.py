"""Pure text normalization helpers for Worker Log prose streams."""

from __future__ import annotations

import re

_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_GLUED_SENTENCE_RE = re.compile(r'(?<=[.!?:])(?=[A-Z][a-z]{2,}\b)')
_INLINE_CODE_RE = re.compile(r'`[^`]+`')

# Beat-starter phrases marking a new Planner thought segment, used to
# insert paragraph breaks when one beat runs directly into the next.
_BEAT_PATTERN = re.compile(
    r"(?<=[a-z)\]'])(?=(?:Now I|Let me|I need to|First[, ]|Next[, ]|Then[, ]|Finally[, ]|I have)\b)"
)


def normalize_worker_log_text(text: str) -> str:
    """Normalize platform newlines without changing streamed word boundaries."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def compact_excess_blank_lines(text: str) -> str:
    """Limit prose spacing to one blank line between sections."""
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", text)


def separate_glued_prose(text: str) -> str:
    """Insert paragraph breaks between glued sentences.

    Skips content inside fenced code blocks (```...```) and inline
    code spans (`...`).  Idempotent — safe to apply repeatedly.
    """
    if not text:
        return text
    lines = text.split('\n')
    result: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith('```'):
            in_fence = not in_fence
            result.append(line)
        elif in_fence:
            result.append(line)
        else:
            result.append(_fix_glue_in_line(line))
    return '\n'.join(result)


def _fix_glue_in_line(line: str) -> str:
    """Apply glued-sentence breaks to one line, skipping inline code."""
    # Gather inline-code spans with unique placeholders.
    blocks: list[str] = []

    def _collect(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f'\x00I{len(blocks) - 1}\x00'

    protected = _INLINE_CODE_RE.sub(_collect, line)
    # Apply glue-breaking.
    fixed = _GLUED_SENTENCE_RE.sub('\n\n', protected)
    # Restore inline-code spans.
    for i, block in enumerate(blocks):
        fixed = fixed.replace(f'\x00I{i}\x00', block)
    return fixed


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


def normalize_assistant_display_text(text: str) -> str:
    """Insert paragraph breaks between adjacent Planner beat segments.

    Preserves fenced code blocks (`` ```...``` ``) and inline code spans
    (`` `...` ``) without damaging their content.  Idempotent — safe to
    apply at any render boundary (streaming flush, finalize_content,
    set_content).

    Handles two glue patterns:
      1. Sentence-boundary glue: ``methods.Now let me`` → ``methods.\n\nNow let me``
      2. Beat-boundary glue without punctuation: ``testsNow I`` → ``tests\n\nNow I``
    """
    if not text:
        return text
    lines = text.split('\n')
    result: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith('```'):
            in_fence = not in_fence
            result.append(line)
        elif in_fence:
            result.append(line)
        else:
            result.append(_fix_assistant_beat_glue(line))
    return '\n'.join(result)


def _fix_assistant_beat_glue(line: str) -> str:
    """Insert paragraph breaks in one line, protecting inline code spans."""
    if not line:
        return line
    # Protect inline code spans via placeholder substitution
    blocks: list[str] = []

    def _collect(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f'\x00I{len(blocks) - 1}\x00'

    protected = _INLINE_CODE_RE.sub(_collect, line)
    # Apply sentence-boundary separation (existing logic)
    fixed = _GLUED_SENTENCE_RE.sub('\n\n', protected)
    # Apply beat-boundary separation
    fixed = _BEAT_PATTERN.sub('\n\n', fixed)
    # Restore inline code spans
    for i, block in enumerate(blocks):
        fixed = fixed.replace(f'\x00I{i}\x00', block)
    return fixed
