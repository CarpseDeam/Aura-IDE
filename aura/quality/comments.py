from __future__ import annotations

import re

_TRIGGER_WORDS = [
    "Initialize",
    "Loop through",
    "Iterate over",
    "Check if",
    "Set",
    "Get",
    "Return",
    "Create",
    "Update",
    "Call",
    "Store",
    "Append",
    "Define",
    "This function",
    "Import",
    "Configure",
    "Handle",
    "Process",
    "Calculate",
    "Convert",
    "Load",
    "Save",
]

_TRIGGER_PATTERN = re.compile(
    r"^\s*#\s+(?:" + "|".join(re.escape(w) + r"\b" for w in _TRIGGER_WORDS) + r")\s",
    re.IGNORECASE,
)

_PRESERVE_PATTERNS = [
    re.compile(r"# noqa\b"),
    re.compile(r"# type:\s*ignore\b"),
    re.compile(r"# pyright:"),
    re.compile(r"# mypy:"),
    re.compile(r"# ruff:"),
    re.compile(r"# TODO\b"),
    re.compile(r"# FIXME\b"),
    re.compile(r"# NOTE\b"),
    re.compile(r"# WARNING\b"),
    re.compile(r"# HACK\b"),
    re.compile(r"# BUG\b"),
    re.compile(r"# XXX\b"),
    re.compile(r"https?://"),
]
_PRESERVE_SUBSTRINGS = ["copyright", "license", "author", "all rights reserved"]


def remove_ai_filler_comments(code: str) -> tuple[str, int]:
    lines = code.splitlines(keepends=True)
    removed = 0
    kept: list[str] = []

    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            kept.append(line)
            continue

        # Shebang lines are always preserved
        if stripped.startswith("#!"):
            kept.append(line)
            continue

        # Check preserve patterns
        if _is_preserved(stripped):
            kept.append(line)
            continue

        if _TRIGGER_PATTERN.match(stripped):
            removed += 1
            continue

        kept.append(line)

    return ("".join(kept), removed)


def _is_preserved(comment_line: str) -> bool:
    for pat in _PRESERVE_PATTERNS:
        if pat.search(comment_line):
            return True
    lower = comment_line.lower()
    for substr in _PRESERVE_SUBSTRINGS:
        if substr in lower:
            return True
    return False
