from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^\s*```(\w*)\s*$")
_DIFF_MARKER_RE = re.compile(r"^(---\s|\+\+\+\s|@@ )")


def strip_markdown_wrapper(text: str) -> tuple[str, bool]:
    """Strip a single markdown fenced code block, if present."""
    lines = _split_lines(text)

    # Collect indices of all fence lines
    fence_indices = [i for i, line in enumerate(lines) if _FENCE_RE.match(line)]

    if len(fence_indices) != 2:
        return (text, False)

    # Don't strip if the text looks like a unified diff
    if any(_DIFF_MARKER_RE.match(line) for line in lines):
        return (text, False)

    open_idx, close_idx = fence_indices[0], fence_indices[1]

    # Closing fence must come after opening fence
    if close_idx <= open_idx:
        return (text, False)

    # Extract lines between the fences
    inner = lines[open_idx + 1 : close_idx]
    stripped = _join_lines(inner)
    return (stripped, True)


def _split_lines(text: str) -> list[str]:
    return text.splitlines(keepends=False)


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines)
