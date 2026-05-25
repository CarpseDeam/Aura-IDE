"""Approval-gated write tools: write_file, edit_file, patch_file."""
from __future__ import annotations

import difflib
import re
from pathlib import Path
import hashlib
from typing import Any

from aura.paths import safe_is_relative_to, safe_relative_to


def _rel_path(workspace_root: Path, target: Path) -> str:
    if safe_is_relative_to(target, workspace_root):
        return safe_relative_to(target, workspace_root).as_posix()
    return str(target)


def _failure_payload(
    workspace_root: Path,
    target: Path,
    error: str,
    failure_class: str,
    **extra: Any,
) -> dict[str, Any]:
    rel = _rel_path(workspace_root, target)
    payload: dict[str, Any] = {
        "ok": False,
        "path": rel,
        "rel_path": rel,
        "error": error,
        "failure_class": failure_class,
    }
    payload.update(extra)
    return payload


def _stale_line_range_payload(
    workspace_root: Path,
    target: Path,
    error: str,
    start_line: int,
    end_line: int,
) -> dict[str, Any]:
    return _failure_payload(
        workspace_root,
        target,
        error,
        "edit_mechanics_stale_line_range",
        suggested_tool="read_file",
        suggested_next_tool="read_file",
        suggested_next_action="Re-read the file, then retry edit_line_range with corrected line numbers.",
        start_line=start_line,
        end_line=end_line,
    )


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
    rel = _rel_path(workspace_root, target)
    if target.exists() and target.is_file():
        try:
            old_content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "path": rel,
                "rel_path": rel,
                "old_content": "",
                "new_content": content,
                "is_new_file": False,
                "error": "file is not valid UTF-8 text",
                "failure_class": "internal_error",
            }
    else:
        old_content = ""
    return {
        "ok": True,
        "path": rel,
        "rel_path": rel,
        "old_content": old_content,
        "new_content": content,
        "is_new_file": not target.exists(),
    }


def propose_line_range_edit(
    workspace_root: Path,
    target: Path,
    start_line: int,
    end_line: int,
    new_str: str,
    expected_old_str: str | None = None,
    expected_old_hash: str | None = None,
) -> dict[str, Any]:
    """Propose replacing an exact line range in a file.

    1-based, inclusive start_line, exclusive end_line (replaces lines
    [start_line, end_line)). When start_line == end_line, inserts before
    that line. start_line == end_line == num_lines + 1 appends at EOF.
    Requires the file to already exist.
    """
    if not target.exists():
        rel = _rel_path(workspace_root, target)
        return _failure_payload(
            workspace_root,
            target,
            f"file not found: {rel}",
            "path_error",
            suggested_tool="write_file",
            suggested_next_tool="write_file",
            suggested_next_action="Use write_file if this file should be created.",
        )
    if not target.is_file():
        rel = _rel_path(workspace_root, target)
        return _failure_payload(workspace_root, target, f"not a regular file: {rel}", "path_error")

    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _failure_payload(workspace_root, target, "file is not valid UTF-8 text", "internal_error")
    except OSError:
        return _failure_payload(workspace_root, target, "failed to read file", "internal_error")

    rel = _rel_path(workspace_root, target)
    lines_with_nl = original.splitlines(keepends=True)
    num_lines = len(lines_with_nl)

    # Validate line numbers
    if start_line < 1:
        return _stale_line_range_payload(workspace_root, target, f"start_line must be >= 1, got {start_line}", start_line, end_line)
    if end_line < start_line:
        return _stale_line_range_payload(workspace_root, target, f"end_line ({end_line}) must be >= start_line ({start_line})", start_line, end_line)
    if start_line > num_lines + 1:
        return _stale_line_range_payload(workspace_root, target, f"start_line ({start_line}) exceeds file length+1 ({num_lines + 1})", start_line, end_line)
    if end_line > num_lines + 1:
        return _stale_line_range_payload(workspace_root, target, f"end_line ({end_line}) exceeds file length+1 ({num_lines + 1})", start_line, end_line)

    # Convert to 0-based for replace_line_range
    start_idx = start_line - 1
    end_idx = end_line - 1
    current_range = "".join(lines_with_nl[start_idx:end_idx])
    if start_line < end_line and expected_old_str is not None and current_range != expected_old_str:
        return _stale_line_range_payload(
            workspace_root,
            target,
            "Line range content did not match expected_old_str.",
            start_line,
            end_line,
        )
    if start_line < end_line and expected_old_hash is not None:
        current_hash = hashlib.sha256(current_range.encode("utf-8")).hexdigest()
        if current_hash != expected_old_hash:
            return _stale_line_range_payload(
                workspace_root,
                target,
                "Line range content did not match expected_old_hash.",
                start_line,
                end_line,
            )
    new_content = replace_line_range(original, lines_with_nl, start_idx, end_idx, new_str)

    # Validate Python syntax if .py file
    if target.suffix == ".py":
        try:
            compile(new_content, target.name, "exec")
        except SyntaxError as exc:
            return _failure_payload(
                workspace_root,
                target,
                f"replacement produces invalid Python: {exc}",
                "syntax_invalid",
                suggested_tool="edit_line_range",
                suggested_next_tool="edit_line_range",
                suggested_next_action="Repair the Python syntax in this file before any unrelated tool call.",
                start_line=start_line,
                end_line=end_line,
            )

    return {
        "ok": True,
        "path": rel,
        "rel_path": rel,
        "old_content": original,
        "new_content": new_content,
        "is_new_file": False,
        "start_line": start_line,
        "end_line": end_line,
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


def _preview_block(text: str, limit: int = 160) -> str:
    compact = text.replace("\r\n", "\n")
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def propose_patch_file(
    workspace_root: Path,
    target: Path,
    edits: list[dict[str, Any]],
    expected_file_hash: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Propose an atomic multi-hunk exact-text patch for one existing file."""
    rel = _rel_path(workspace_root, target)
    if not target.exists():
        return _failure_payload(workspace_root, target, f"file not found: {rel}", "path_error")
    if not target.is_file():
        return _failure_payload(workspace_root, target, f"not a regular file: {rel}", "path_error")
    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _failure_payload(workspace_root, target, "file is not valid UTF-8 text", "internal_error")
    except OSError:
        return _failure_payload(workspace_root, target, "failed to read file", "internal_error")

    if expected_file_hash is not None:
        current_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        if current_hash != expected_file_hash:
            return _failure_payload(
                workspace_root,
                target,
                "File content did not match expected_file_hash.",
                "patch_file_hash_mismatch",
                suggested_next_action="Re-read the file and submit one corrected patch_file transaction.",
            )

    proposed = original
    for index, hunk in enumerate(edits):
        old = hunk.get("old")
        new = hunk.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            return _failure_payload(
                workspace_root,
                target,
                "Each patch_file hunk must include string old and new fields.",
                "internal_error",
                hunk_index=index,
            )
        if old == "":
            return _failure_payload(
                workspace_root,
                target,
                "patch_file hunk old block must not be empty.",
                "internal_error",
                hunk_index=index,
            )

        explicit_occurrence = "occurrence" in hunk
        occurrence = hunk.get("occurrence", 1)
        allow_multiple = bool(hunk.get("allow_multiple", False))
        if not isinstance(occurrence, int) or occurrence < 1:
            return _failure_payload(
                workspace_root,
                target,
                "patch_file hunk occurrence must be a 1-based integer.",
                "internal_error",
                hunk_index=index,
            )

        count = proposed.count(old)
        if count == 0:
            return _failure_payload(
                workspace_root,
                target,
                "patch_file hunk old block was not found.",
                "patch_hunk_not_found",
                hunk_index=index,
                old_preview=_preview_block(old),
                suggested_next_action="Re-read the file and submit one corrected patch_file transaction.",
            )
        if count > 1 and not allow_multiple and not explicit_occurrence:
            return _failure_payload(
                workspace_root,
                target,
                "patch_file hunk old block is ambiguous.",
                "patch_hunk_ambiguous",
                hunk_index=index,
                old_preview=_preview_block(old),
                occurrence_count=count,
                suggested_next_action="Provide occurrence or make the old block more specific.",
            )

        if allow_multiple and not explicit_occurrence:
            proposed = proposed.replace(old, new)
            continue

        if occurrence > count:
            return _failure_payload(
                workspace_root,
                target,
                "patch_file hunk occurrence exceeds matching old block count.",
                "patch_hunk_not_found",
                hunk_index=index,
                old_preview=_preview_block(old),
                occurrence_count=count,
                suggested_next_action="Re-read the file and submit one corrected patch_file transaction.",
            )
        start = -1
        search_from = 0
        for _ in range(occurrence):
            start = proposed.find(old, search_from)
            search_from = start + len(old)
        proposed = proposed[:start] + new + proposed[start + len(old):]

    if target.suffix == ".py":
        try:
            compile(proposed, target.name, "exec")
        except SyntaxError as exc:
            return _failure_payload(
                workspace_root,
                target,
                f"replacement produces invalid Python: {exc}",
                "syntax_invalid",
                suggested_tool="patch_file",
                suggested_next_tool="patch_file",
                suggested_next_action="Repair the Python syntax in this file before any unrelated tool call.",
            )

    return {
        "ok": True,
        "path": rel,
        "rel_path": rel,
        "old_content": original,
        "new_content": proposed,
        "is_new_file": False,
        "hunk_count": len(edits),
        "description": description or "",
    }


def propose_edit(
    workspace_root: Path, target: Path, old_str: str, new_str: str
) -> dict[str, Any]:
    rel = _rel_path(workspace_root, target)
    if not target.exists():
        return _failure_payload(workspace_root, target, f"file not found: {rel}", "path_error")
    if not target.is_file():
        return _failure_payload(workspace_root, target, f"not a regular file: {rel}", "path_error")
    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _failure_payload(workspace_root, target, "file is not valid UTF-8 text", "internal_error")

    # Sanitize inputs: strip markdown fences, normalize whitespace.
    old_str, new_str, sanitized = _sanitize_edit_strings(old_str, new_str)

    # ---- Tier 1: Exact string match (fast path, backward compatible) ----
    occurrences = original.count(old_str)
    if occurrences == 1:
        proposed = original.replace(old_str, new_str, 1)
        result: dict[str, Any] = {
            "ok": True,
            "path": rel,
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
            "path": rel,
            "rel_path": rel,
            "error": (
                "old_str not found in file. Best fuzzy match ratio: 0.000 "
                "(threshold: 0.75). Tried exact, line-exact, and fuzzy matching."
            ),
            "failure_class": "edit_mechanics_old_str_not_found",
            "edit_file_failure": True,
            "suggested_tool": "edit_line_range",
            "suggested_next_tool": "edit_line_range",
            "suggested_next_action": "Re-read the file to see the actual content, then use edit_line_range with the exact line numbers you can see.",
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
            "path": rel,
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
    candidates: list[tuple[int, float]] = []
    best_ratio = 0.0
    all_near_matches: list[tuple[int, float]] = []  # for nearest_candidates

    if len(old_lines) <= len(file_lines):
        normalized_old = [line.strip() for line in old_lines]
        normalized_old_block = "\n".join(normalized_old)

        for i in range(len(file_lines) - len(old_lines) + 1):
            window = file_lines[i:i + len(old_lines)]
            normalized_window = [line.strip() for line in window]
            normalized_window_block = "\n".join(normalized_window)
            ratio = difflib.SequenceMatcher(
                None, normalized_old_block, normalized_window_block
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
            if ratio >= 0.75:
                candidates.append((i, ratio))
            if ratio > 0.5:
                all_near_matches.append((i, ratio))

    def _build_nearest_candidates() -> list[dict[str, Any]]:
        """Build nearest_candidates list from all_near_matches."""
        sorted_matches = sorted(all_near_matches, key=lambda x: -x[1])
        result = []
        seen = set()
        for idx, rat in sorted_matches:
            block_text = "\n".join(file_lines[idx:idx + window_len])
            key = (idx, idx + window_len)
            if key not in seen:
                seen.add(key)
                result.append({
                    "start_line": idx + 1,
                    "end_line": idx + window_len,
                    "text": block_text,
                })
            if len(result) >= 3:
                break
        return result

    if len(candidates) == 1:
        start_idx = candidates[0][0]
        proposed = replace_line_range(
            original, lines_with_nl, start_idx, start_idx + len(old_lines), new_str
        )
        result: dict[str, Any] = {
            "ok": True,
            "path": rel,
            "rel_path": rel,
            "old_content": original,
            "new_content": proposed,
            "is_new_file": False,
            "match_tier": "fuzzy",
            "fuzzy_ratio": round(candidates[0][1], 3),
        }
        if sanitized:
            result["sanitized"] = True
        return result

    if len(candidates) > 1:
        max_ratio = max(r for _, r in candidates)
        top_candidates = [(i, r) for i, r in candidates if max_ratio - r < 0.001]
        if len(top_candidates) == 1:
            start_idx = top_candidates[0][0]
            proposed = replace_line_range(
                original, lines_with_nl, start_idx, start_idx + len(old_lines), new_str
            )
            result: dict[str, Any] = {
                "ok": True,
                "path": rel,
                "rel_path": rel,
                "old_content": original,
                "new_content": proposed,
                "is_new_file": False,
                "match_tier": "fuzzy",
                "fuzzy_ratio": round(max_ratio, 3),
            }
            if sanitized:
                result["sanitized"] = True
            return result

        # Multiple top candidates — ambiguous
        line_count = len(old_lines)
        lines_detail = "\n".join(
            f"  Candidate {j+1}: lines {start+1}-{start+line_count}"
            for j, (start, _) in enumerate(top_candidates)
        )
        error_msg = (
            f"ambiguous: old_str matches {len(top_candidates)} blocks "
            f"in the file (best ratio: {max_ratio:.3f}).\n"
            f"{lines_detail}\n"
            f"old_str does not uniquely identify the target. "
            f"Add more surrounding context lines to disambiguate."
        )
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "error": error_msg,
            "failure_class": "edit_mechanics_ambiguous_match",
            "edit_file_failure": True,
            "suggested_tool": "edit_line_range",
            "suggested_next_tool": "edit_line_range",
            "suggested_next_action": "Re-read the file to see the actual content, then use edit_line_range with the exact line numbers you can see.",
            "best_fuzzy_ratio": round(max_ratio, 3),
            "nearest_candidates": _build_nearest_candidates(),
        }

    # ---- All tiers failed ----
    error_msg = (
        f"old_str not found in file. Best fuzzy match ratio: {best_ratio:.3f} "
        f"(threshold: 0.75). Tried exact, line-exact, and fuzzy matching."
    )
    return {
        "ok": False,
        "path": rel,
        "rel_path": rel,
        "error": error_msg,
        "failure_class": "edit_mechanics_old_str_not_found",
        "edit_file_failure": True,
        "suggested_tool": "edit_line_range",
        "suggested_next_tool": "edit_line_range",
        "suggested_next_action": "Re-read the file to see the actual content, then use edit_line_range with the exact line numbers you can see.",
        "best_fuzzy_ratio": round(best_ratio, 3),
        "best_ratio": round(best_ratio, 4),
        "nearest_candidates": _build_nearest_candidates(),
    }
