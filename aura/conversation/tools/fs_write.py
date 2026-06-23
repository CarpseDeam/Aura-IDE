"""Approval-gated write helpers for write_file and patch_file."""
from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any

from aura.conversation.tools._replacement_engine import apply_replacement_to_content, _preview_block, _sanitize_edit_strings

from aura.conversation.tools.fs_read import read_file_snapshot
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


def _proposal_context(text: str, line: int | None, radius: int = 4) -> dict:
    lines = str(text).splitlines()
    error_line = line if isinstance(line, int) and line > 0 else None
    if not lines:
        return {
            "error_line": error_line,
            "start_line": 0,
            "end_line": 0,
            "lines": [],
        }

    context_line = min(error_line or 1, len(lines))
    radius = max(0, radius)
    start_line = max(1, context_line - radius)
    end_line = min(len(lines), context_line + radius)
    return {
        "error_line": error_line,
        "start_line": start_line,
        "end_line": end_line,
        "lines": [
            {"line": number, "text": lines[number - 1]}
            for number in range(start_line, end_line + 1)
        ],
    }


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
        suggested_next_action="Re-read the file, then retry patch_file with current exact text.",
        start_line=start_line,
        end_line=end_line,
    )


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
                suggested_tool="patch_file",
                suggested_next_tool="patch_file",
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


def propose_patch_file(
    workspace_root: Path,
    target: Path,
    edits: list[dict[str, Any]],
    expected_file_hash: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Propose an atomic multi-hunk patch for one existing file."""
    rel = _rel_path(workspace_root, target)
    if not target.exists():
        return _failure_payload(workspace_root, target, f"file not found: {rel}", "path_error")
    if not target.is_file():
        return _failure_payload(workspace_root, target, f"not a regular file: {rel}", "path_error")
    try:
        original, current_hash, _file_size = read_file_snapshot(target)
    except UnicodeDecodeError:
        return _failure_payload(workspace_root, target, "file is not valid UTF-8 text", "internal_error")
    except OSError:
        return _failure_payload(workspace_root, target, "failed to read file", "internal_error")

    if expected_file_hash is not None:
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

        match = apply_replacement_to_content(
            proposed,
            old,
            new,
            occurrence=occurrence if explicit_occurrence else None,
            allow_multiple=allow_multiple and not explicit_occurrence,
            raw_first=True,
            exact_duplicates_are_ambiguous=True,
            normalize_replacement_newlines=True,
        )
        if not match.get("ok"):
            reason = str(match.get("reason") or "not_found")
            failure_class = "patch_hunk_ambiguous" if reason == "ambiguous" else "patch_hunk_not_found"
            error = (
                "patch_file hunk old block is ambiguous."
                if failure_class == "patch_hunk_ambiguous"
                else "patch_file hunk old block was not found."
            )
            if (
                failure_class == "patch_hunk_not_found"
                and explicit_occurrence
                and int(match.get("occurrence_count") or 0) > 0
            ):
                error = "patch_file hunk occurrence exceeds matching old block count."
            extra: dict[str, Any] = {
                "hunk_index": index,
                "old_preview": _preview_block(str(match.get("old") or old)),
                "suggested_next_action": (
                    "Provide occurrence or make the old block more specific."
                    if failure_class == "patch_hunk_ambiguous"
                    else "Re-read the file and submit one corrected patch_file transaction."
                ),
            }
            for key in (
                "match_tier",
                "best_fuzzy_ratio",
                "best_ratio",
                "fuzzy_ratio",
                "nearest_candidates",
                "occurrence_count",
                "sanitized",
                "sanitized_fallback",
            ):
                if key in match:
                    extra[key] = match[key]
            return _failure_payload(
                workspace_root,
                target,
                error,
                failure_class,
                **extra,
            )
        proposed = str(match["content"])

    if target.suffix == ".py":
        try:
            compile(proposed, target.name, "exec")
        except SyntaxError as exc:
            syntax_line = exc.lineno if isinstance(exc.lineno, int) else None
            extra: dict[str, Any] = {
                "suggested_tool": "patch_file",
                "suggested_next_tool": "patch_file",
                "suggested_next_action": (
                    "Re-read the current file and inspect proposed_context. Treat joined Python statements "
                    "or swallowed newlines as a likely patch boundary issue. Retry patch_file with a larger "
                    "enclosing block: the line before, the edited lines, and the line after. Use the current "
                    "expected_file_hash. Keep existing-file recovery on patch_file; do not use write_file as "
                    "a fallback for this existing-file edit."
                ),
                "proposed_context": _proposal_context(proposed, syntax_line),
            }
            if syntax_line is not None:
                extra["syntax_error_line"] = syntax_line
            if isinstance(exc.offset, int):
                extra["syntax_error_offset"] = exc.offset
            if isinstance(exc.text, str):
                extra["syntax_error_text"] = exc.text.rstrip("\r\n")
            return _failure_payload(
                workspace_root,
                target,
                f"replacement produces invalid Python: {exc}",
                "syntax_invalid",
                **extra,
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

    match = apply_replacement_to_content(original, old_str, new_str)
    if match.get("ok"):
        result: dict[str, Any] = {
            "ok": True,
            "path": rel,
            "rel_path": rel,
            "old_content": original,
            "new_content": str(match["content"]),
            "is_new_file": False,
            "match_tier": match.get("match_tier", "exact"),
        }
        for key in ("fuzzy_ratio", "sanitized"):
            if key in match:
                result[key] = match[key]
        return result

    failure_class = (
        "edit_mechanics_ambiguous_match"
        if match.get("reason") == "ambiguous"
        else "edit_mechanics_old_str_not_found"
    )
    payload: dict[str, Any] = {
        "ok": False,
        "path": rel,
        "rel_path": rel,
        "error": str(match.get("error") or "old_str not found in file."),
        "failure_class": failure_class,
        "edit_file_failure": True,
        "suggested_tool": "patch_file",
        "suggested_next_tool": "patch_file",
        "suggested_next_action": "Re-read the file to see the actual content, then use patch_file with current exact text.",
    }
    for key in (
        "best_fuzzy_ratio",
        "best_ratio",
        "nearest_candidates",
        "match_tier",
        "occurrence_count",
        "sanitized",
    ):
        if key in match:
            payload[key] = match[key]
    return payload
