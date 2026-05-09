"""Approval-gated write tools: write_file, edit_file."""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any


def _sanitize_edit_strings(old_str: str, new_str: str) -> tuple[str, str, bool]:
    """Strip markdown fences and normalize whitespace on edit strings.

    Strips leading/trailing whitespace from both strings, then detects and
    removes a single pair of surrounding markdown fences (``` ... ```) from
    *old_str* only. After fence removal, trailing newlines are stripped from
    *old_str* (not *new_str* — the caller may intend a specific trailing
    newline in the replacement).

    Returns:
        (sanitized_old, sanitized_new, was_sanitized):
        - sanitized_old:  old_str after whitespace / fence stripping.
        - sanitized_new:  new_str after leading/trailing whitespace strip.
        - was_sanitized:  True if any modification was applied.
    """
    sanitized = False

    old = old_str.strip()
    new = new_str.strip()

    if old != old_str:
        sanitized = True
    if new != new_str:
        sanitized = True

    # Detect and remove a single pair of outermost markdown fences from old_str.
    # A fence line: optional whitespace, then 3+ backticks, optionally followed
    # by a language tag (for opening) or nothing (for closing).
    lines = old.split("\n")
    if len(lines) >= 2:
        first = lines[0]
        last = lines[-1]
        open_match = re.match(r"^(\s*)(`{3,})(?:\s*\w*)?\s*$", first)
        close_match = re.match(r"^(\s*)(`{3,})\s*$", last)
        if open_match and close_match and open_match.group(2) == close_match.group(2):
            old = "\n".join(lines[1:-1])
            sanitized = True

    # Re-strip trailing newlines from old_str only.
    old = old.rstrip("\n")

    return old, new, sanitized


def propose_write(workspace_root: Path, target: Path, content: str) -> dict[str, Any]:
    rel = target.relative_to(workspace_root).as_posix() if target.is_relative_to(workspace_root) else str(target)
    if target.exists() and target.is_file():
        try:
            old_content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "rel_path": rel,
                "old_content": "",
                "new_content": content,
                "is_new_file": False,
                "error": "file is not valid UTF-8 text",
            }
    else:
        old_content = ""
    return {
        "ok": True,
        "rel_path": rel,
        "old_content": old_content,
        "new_content": content,
        "is_new_file": not target.exists(),
    }


def replace_line_range(
    original: str, file_lines_with_newlines: list[str], start_line: int, end_line: int, new_str: str
) -> str:
    """Replace lines [start_line, end_line) in original with new_str.

    file_lines_with_newlines must come from original.splitlines(keepends=True)
    so each element retains its trailing newline (or lack thereof for the last line).
    """
    start_char = sum(len(ln) for ln in file_lines_with_newlines[:start_line])
    end_char = start_char + sum(len(ln) for ln in file_lines_with_newlines[start_line:end_line])
    return original[:start_char] + new_str + original[end_char:]


def propose_edit(
    workspace_root: Path, target: Path, old_str: str, new_str: str
) -> dict[str, Any]:
    if not target.exists():
        return {"ok": False, "error": f"file not found: {target.relative_to(workspace_root)}"}
    if not target.is_file():
        return {"ok": False, "error": f"not a regular file: {target.relative_to(workspace_root)}"}
    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "file is not valid UTF-8 text"}

    rel = target.relative_to(workspace_root).as_posix()

    # Sanitize inputs: strip markdown fences, normalize whitespace.
    old_str, new_str, sanitized = _sanitize_edit_strings(old_str, new_str)

    # ---- Tier 1: Exact string match (fast path, backward compatible) ----
    occurrences = original.count(old_str)
    if occurrences == 1:
        proposed = original.replace(old_str, new_str, 1)
        result: dict[str, Any] = {
            "ok": True,
            "rel_path": rel,
            "old_content": original,
            "new_content": proposed,
            "is_new_file": False,
            "match_tier": "exact",
        }
        if sanitized:
            result["sanitized"] = True
        return result

    # Prepare line-based structures for Tiers 2 & 3.
    lines_with_nl = original.splitlines(keepends=True)
    file_lines = original.splitlines()
    old_lines = old_str.splitlines()

    if not old_lines:
        # empty old_str after splitting — fall through to error below
        return {
            "ok": False,
            "error": (
                "old_str not found in file. Best fuzzy match ratio: 0.000 "
                "(threshold: 0.75). Tried exact, line-exact, and fuzzy matching."
            ),
        }

    # ---- Tier 2: Line-by-line exact match ----
    line_matches: list[int] = []
    window_len = len(old_lines)
    if window_len <= len(file_lines):
        for i in range(len(file_lines) - window_len + 1):
            if file_lines[i:i + window_len] == old_lines:
                line_matches.append(i)

    if len(line_matches) == 1:
        start_idx = line_matches[0]
        proposed = replace_line_range(original, lines_with_nl, start_idx, start_idx + window_len, new_str)
        result = {
            "ok": True,
            "rel_path": rel,
            "old_content": original,
            "new_content": proposed,
            "is_new_file": False,
            "match_tier": "line_exact",
        }
        if sanitized:
            result["sanitized"] = True
        return result

    # ---- Tier 3: Whitespace-agnostic fuzzy line matching ----
    if len(old_lines) > len(file_lines):
        best_ratio = 0.0
    else:
        # Normalize old_lines once (strip leading/trailing whitespace from each line)
        # for the comparison, while keeping the original block structure.
        normalized_old = [line.strip() for line in old_lines]
        normalized_old_block = "\n".join(normalized_old)

        best_ratio = 0.0
        best_start = -1

        for i in range(len(file_lines) - len(old_lines) + 1):
            window = file_lines[i:i + len(old_lines)]
            normalized_window = [line.strip() for line in window]
            normalized_window_block = "\n".join(normalized_window)
            ratio = difflib.SequenceMatcher(
                None, normalized_old_block, normalized_window_block
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i

        if best_ratio >= 0.75:
            proposed = replace_line_range(
                original, lines_with_nl, best_start, best_start + len(old_lines), new_str
            )
            result = {
                "ok": True,
                "rel_path": rel,
                "old_content": original,
                "new_content": proposed,
                "is_new_file": False,
                "match_tier": "fuzzy",
                "fuzzy_ratio": round(best_ratio, 3),
            }
            if sanitized:
                result["sanitized"] = True
            return result

    # ---- All tiers failed ----
    error_msg = (
        f"old_str not found in file. Best fuzzy match ratio: {best_ratio:.3f} "
        f"(threshold: 0.75). Tried exact, line-exact, and fuzzy matching."
    )
    return {
        "ok": False,
        "error": error_msg,
    }
