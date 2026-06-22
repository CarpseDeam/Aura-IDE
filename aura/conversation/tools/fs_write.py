"""Approval-gated write helpers for write_file and patch_file."""
from __future__ import annotations

import difflib
import re
from pathlib import Path
import hashlib
from typing import Any

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


def _preview_block(text: str, limit: int = 160) -> str:
    compact = text.replace("\r\n", "\n")
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _find_all_spans(text: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    if not needle:
        return spans
    start = text.find(needle)
    while start >= 0:
        spans.append((start, start + len(needle)))
        start = text.find(needle, start + len(needle))
    return spans


def _line_offsets(lines_with_nl: list[str]) -> list[int]:
    offsets: list[int] = []
    current = 0
    for line in lines_with_nl:
        offsets.append(current)
        current += len(line)
    offsets.append(current)
    return offsets


def _dominant_newline(text: str, fallback: str = "\n") -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    if lf > 0:
        return "\n"
    return fallback


def _normalize_newlines(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def _map_offset_norm_to_orig(original: str, norm_offset: int) -> int:
    """Map a character offset in normalized (LF-only) content back to original content.

    Normalization converts \r\n → \n (2 chars → 1 char) and \r → \n (1→1).
    Only \r\n causes a length discrepancy, so walk the original string and
    advance orig_idx by 2 for each \r\n while norm_count advances by 1.
    """
    orig_idx = 0
    norm_count = 0
    while norm_count < norm_offset and orig_idx < len(original):
        if original.startswith("\r\n", orig_idx):
            orig_idx += 2
            norm_count += 1
        else:
            orig_idx += 1
            norm_count += 1
    return orig_idx


def _replace_span(text: str, start: int, end: int, new: str) -> str:
    return text[:start] + new + text[end:]


def _replacement_for_span(
    content: str,
    start: int,
    end: int,
    new: str,
    *,
    normalize_replacement_newlines: bool,
) -> str:
    if not normalize_replacement_newlines:
        return new
    fallback = _dominant_newline(content)
    newline = _dominant_newline(content[start:end], fallback=fallback)
    return _normalize_newlines(new, newline)


def _replace_spans(
    content: str,
    spans: list[tuple[int, int]],
    new: str,
    *,
    normalize_replacement_newlines: bool,
) -> str:
    updated = content
    for start, end in reversed(spans):
        replacement = _replacement_for_span(
            content,
            start,
            end,
            new,
            normalize_replacement_newlines=normalize_replacement_newlines,
        )
        updated = _replace_span(updated, start, end, replacement)
    return updated


def _span_line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _line_context(text: str, start: int, end: int, limit: int = 2) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    start_line = _span_line_number(text, start)
    end_line = _span_line_number(text, end)
    before = max(1, start_line - limit)
    after = min(len(lines), end_line + limit)
    return "\n".join(lines[before - 1:after])


def _build_span_candidates(
    text: str,
    spans: list[tuple[int, int]],
    limit: int = 3,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for start, end in spans:
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "start_line": _span_line_number(text, start),
                "end_line": _span_line_number(text, max(start, end - 1)),
                "text": _preview_block(_line_context(text, start, end)),
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def _build_line_candidates(
    file_lines: list[str],
    line_matches: list[int],
    window_len: int,
    limit: int = 3,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for idx in line_matches:
        key = (idx, idx + window_len)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "start_line": idx + 1,
                "end_line": idx + window_len,
                "text": "\n".join(file_lines[idx:idx + window_len]),
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def _not_found_replacement(
    *,
    best_ratio: float,
    nearest_candidates: list[dict[str, Any]],
    match_tier: str = "fuzzy",
    occurrence_count: int | None = None,
    old: str = "",
    sanitized: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "reason": "not_found",
        "match_tier": match_tier,
        "old": old,
        "error": (
            f"old_str not found in file. Best fuzzy match ratio: {best_ratio:.3f} "
            f"(threshold: 0.75). Tried exact, line-exact, and fuzzy matching."
        ),
        "best_fuzzy_ratio": round(best_ratio, 3),
        "best_ratio": round(best_ratio, 4),
        "nearest_candidates": nearest_candidates,
    }
    if occurrence_count is not None:
        result["occurrence_count"] = occurrence_count
    if sanitized:
        result["sanitized"] = True
    return result


def _ambiguous_replacement(
    *,
    error: str,
    match_tier: str,
    occurrence_count: int,
    nearest_candidates: list[dict[str, Any]],
    old: str,
    sanitized: bool,
    best_ratio: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "reason": "ambiguous",
        "match_tier": match_tier,
        "old": old,
        "error": error,
        "occurrence_count": occurrence_count,
        "nearest_candidates": nearest_candidates,
    }
    if best_ratio is not None:
        result["best_fuzzy_ratio"] = round(best_ratio, 3)
        result["best_ratio"] = round(best_ratio, 4)
    if sanitized:
        result["sanitized"] = True
    return result


def apply_replacement_to_content(
    content: str,
    old_str: str,
    new_str: str,
    *,
    occurrence: int | None = None,
    allow_multiple: bool = False,
    sanitize: bool = True,
    raw_first: bool = False,
    exact_duplicates_are_ambiguous: bool = False,
    normalize_replacement_newlines: bool = False,
) -> dict[str, Any]:
    """Apply one old/new replacement to an in-memory content string.

    The matching tiers mirror edit_file: exact string, exact line window,
    then whitespace-tolerant fuzzy line matching with ambiguity rejection.
    """
    if raw_first:
        raw_result = apply_replacement_to_content(
            content,
            old_str,
            new_str,
            occurrence=occurrence,
            allow_multiple=allow_multiple,
            sanitize=False,
            raw_first=False,
            exact_duplicates_are_ambiguous=exact_duplicates_are_ambiguous,
            normalize_replacement_newlines=normalize_replacement_newlines,
        )
        if (
            raw_result.get("ok")
            or raw_result.get("reason") == "ambiguous"
            or raw_result.get("occurrence_count")
        ):
            return raw_result
        sanitized_result = apply_replacement_to_content(
            content,
            old_str,
            new_str,
            occurrence=occurrence,
            allow_multiple=allow_multiple,
            sanitize=sanitize,
            raw_first=False,
            exact_duplicates_are_ambiguous=exact_duplicates_are_ambiguous,
            normalize_replacement_newlines=normalize_replacement_newlines,
        )
        if sanitized_result.get("sanitized"):
            sanitized_result["sanitized_fallback"] = True
        return sanitized_result

    if sanitize:
        old, new, sanitized = _sanitize_edit_strings(old_str, new_str)
    else:
        old, new, sanitized = old_str, new_str, False

    # ---- CRLF-aware matching: normalize newlines for matching only ----
    _norm_content = content.replace("\r\n", "\n").replace("\r", "\n")
    _norm_old = old.replace("\r\n", "\n").replace("\r", "\n")
    _crlf_present = (_norm_content != content)

    _match_content = _norm_content if _crlf_present else content
    _match_old = _norm_old if _crlf_present else old
    _use_normalize = normalize_replacement_newlines or _crlf_present

    if old == "":
        return _not_found_replacement(
            best_ratio=0.0,
            nearest_candidates=[],
            old=old,
            sanitized=sanitized,
        )

    # ---- Tier 1: Exact string match ----
    exact_spans = _find_all_spans(_match_content, _match_old)
    if occurrence is not None and exact_spans:
        if occurrence <= len(exact_spans):
            start, end = exact_spans[occurrence - 1]
            if _crlf_present:
                start = _map_offset_norm_to_orig(content, start)
                end = _map_offset_norm_to_orig(content, end)
            replacement = _replacement_for_span(
                content,
                start,
                end,
                new,
                normalize_replacement_newlines=_use_normalize,
            )
            result: dict[str, Any] = {
                "ok": True,
                "content": _replace_span(content, start, end, replacement),
                "match_tier": "exact",
                "occurrence_count": len(exact_spans),
            }
            if sanitized:
                result["sanitized"] = True
            return result
        return _not_found_replacement(
            best_ratio=0.0,
            nearest_candidates=_build_span_candidates(content, exact_spans),
            match_tier="exact",
            occurrence_count=len(exact_spans),
            old=old,
            sanitized=sanitized,
        )
    if len(exact_spans) == 1:
        start, end = exact_spans[0]
        if _crlf_present:
            start = _map_offset_norm_to_orig(content, start)
            end = _map_offset_norm_to_orig(content, end)
        replacement = _replacement_for_span(
            content,
            start,
            end,
            new,
            normalize_replacement_newlines=_use_normalize,
        )
        result = {
            "ok": True,
            "content": _replace_span(content, start, end, replacement),
            "match_tier": "exact",
        }
        if sanitized:
            result["sanitized"] = True
        return result
    if len(exact_spans) > 1:
        if allow_multiple:
            if _crlf_present:
                mapped_spans = [(_map_offset_norm_to_orig(content, s), _map_offset_norm_to_orig(content, e)) for s, e in exact_spans]
            else:
                mapped_spans = exact_spans
            result = {
                "ok": True,
                "content": _replace_spans(
                    content,
                    mapped_spans,
                    new,
                    normalize_replacement_newlines=_use_normalize,
                ),
                "match_tier": "exact",
                "occurrence_count": len(exact_spans),
            }
            if sanitized:
                result["sanitized"] = True
            return result
        if exact_duplicates_are_ambiguous:
            return _ambiguous_replacement(
                error=(
                    "ambiguous: old_str matches multiple exact blocks in the file. "
                    "old_str does not uniquely identify the target."
                ),
                match_tier="exact",
                occurrence_count=len(exact_spans),
                nearest_candidates=_build_span_candidates(content, exact_spans),
                old=old,
                sanitized=sanitized,
            )

    # Prepare line-based structures for Tiers 2 & 3.
    lines_with_nl = _match_content.splitlines(keepends=True)
    file_lines = _match_content.splitlines()
    old_lines = _match_old.splitlines()
    window_len = len(old_lines)

    if not old_lines:
        return _not_found_replacement(
            best_ratio=0.0,
            nearest_candidates=[],
            old=old,
            sanitized=sanitized,
        )

    # ---- Tier 2: Line-by-line exact match ----
    line_matches: list[int] = []
    if window_len <= len(file_lines):
        for i in range(len(file_lines) - window_len + 1):
            if file_lines[i:i + window_len] == old_lines:
                line_matches.append(i)

    if line_matches:
        offsets = _line_offsets(lines_with_nl)
        line_spans = [
            (offsets[start], offsets[start + window_len])
            for start in line_matches
        ]
        if occurrence is not None:
            if occurrence <= len(line_spans):
                start, end = line_spans[occurrence - 1]
                if _crlf_present:
                    start = _map_offset_norm_to_orig(content, start)
                    end = _map_offset_norm_to_orig(content, end)
                replacement = _replacement_for_span(
                    content,
                    start,
                    end,
                    new,
                    normalize_replacement_newlines=_use_normalize,
                )
                result = {
                    "ok": True,
                    "content": _replace_span(content, start, end, replacement),
                    "match_tier": "line_exact",
                    "occurrence_count": len(line_spans),
                }
                if sanitized:
                    result["sanitized"] = True
                return result
            return _not_found_replacement(
                best_ratio=0.0,
                nearest_candidates=_build_line_candidates(file_lines, line_matches, window_len),
                match_tier="line_exact",
                occurrence_count=len(line_spans),
                old=old,
                sanitized=sanitized,
            )
        if len(line_spans) == 1:
            start, end = line_spans[0]
            if _crlf_present:
                start = _map_offset_norm_to_orig(content, start)
                end = _map_offset_norm_to_orig(content, end)
            replacement = _replacement_for_span(
                content,
                start,
                end,
                new,
                normalize_replacement_newlines=_use_normalize,
            )
            result = {
                "ok": True,
                "content": _replace_span(content, start, end, replacement),
                "match_tier": "line_exact",
            }
            if sanitized:
                result["sanitized"] = True
            return result
        if allow_multiple:
            if _crlf_present:
                mapped_spans = [(_map_offset_norm_to_orig(content, s), _map_offset_norm_to_orig(content, e)) for s, e in line_spans]
            else:
                mapped_spans = line_spans
            result = {
                "ok": True,
                "content": _replace_spans(
                    content,
                    mapped_spans,
                    new,
                    normalize_replacement_newlines=_use_normalize,
                ),
                "match_tier": "line_exact",
                "occurrence_count": len(line_spans),
            }
            if sanitized:
                result["sanitized"] = True
            return result
        if exact_duplicates_are_ambiguous:
            return _ambiguous_replacement(
                error=(
                    "ambiguous: old_str matches multiple exact line blocks in the file. "
                    "old_str does not uniquely identify the target."
                ),
                match_tier="line_exact",
                occurrence_count=len(line_spans),
                nearest_candidates=_build_line_candidates(file_lines, line_matches, window_len),
                old=old,
                sanitized=sanitized,
            )

    # ---- Tier 3: Whitespace-agnostic fuzzy line matching ----
    candidates: list[tuple[int, float]] = []
    best_ratio = 0.0
    all_near_matches: list[tuple[int, float]] = []

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
        sorted_matches = sorted(all_near_matches, key=lambda x: -x[1])
        return _build_line_candidates(
            file_lines,
            [idx for idx, _ratio in sorted_matches],
            window_len,
        )

    if occurrence is not None and candidates:
        if occurrence <= len(candidates):
            start_idx, ratio = candidates[occurrence - 1]
            start = _line_offsets(lines_with_nl)[start_idx]
            end = _line_offsets(lines_with_nl)[start_idx + len(old_lines)]
            if _crlf_present:
                start = _map_offset_norm_to_orig(content, start)
                end = _map_offset_norm_to_orig(content, end)
            replacement = _replacement_for_span(
                content,
                start,
                end,
                new,
                normalize_replacement_newlines=_use_normalize,
            )
            result = {
                "ok": True,
                "content": _replace_span(content, start, end, replacement),
                "match_tier": "fuzzy",
                "fuzzy_ratio": round(ratio, 3),
                "occurrence_count": len(candidates),
            }
            if sanitized:
                result["sanitized"] = True
            return result
        return _not_found_replacement(
            best_ratio=best_ratio,
            nearest_candidates=_build_nearest_candidates(),
            match_tier="fuzzy",
            occurrence_count=len(candidates),
            old=old,
            sanitized=sanitized,
        )

    if len(candidates) == 1:
        start_idx, ratio = candidates[0]
        offsets = _line_offsets(lines_with_nl)
        start = offsets[start_idx]
        end = offsets[start_idx + len(old_lines)]
        if _crlf_present:
            start = _map_offset_norm_to_orig(content, start)
            end = _map_offset_norm_to_orig(content, end)
        replacement = _replacement_for_span(
            content,
            start,
            end,
            new,
            normalize_replacement_newlines=_use_normalize,
        )
        result = {
            "ok": True,
            "content": _replace_span(content, start, end, replacement),
            "match_tier": "fuzzy",
            "fuzzy_ratio": round(ratio, 3),
        }
        if sanitized:
            result["sanitized"] = True
        return result

    if len(candidates) > 1:
        max_ratio = max(r for _, r in candidates)
        top_candidates = [(i, r) for i, r in candidates if max_ratio - r < 0.001]
        if len(top_candidates) == 1:
            start_idx = top_candidates[0][0]
            offsets = _line_offsets(lines_with_nl)
            start = offsets[start_idx]
            end = offsets[start_idx + len(old_lines)]
            if _crlf_present:
                start = _map_offset_norm_to_orig(content, start)
                end = _map_offset_norm_to_orig(content, end)
            replacement = _replacement_for_span(
                content,
                start,
                end,
                new,
                normalize_replacement_newlines=_use_normalize,
            )
            result = {
                "ok": True,
                "content": _replace_span(content, start, end, replacement),
                "match_tier": "fuzzy",
                "fuzzy_ratio": round(max_ratio, 3),
            }
            if sanitized:
                result["sanitized"] = True
            return result

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
        return _ambiguous_replacement(
            error=error_msg,
            match_tier="fuzzy",
            occurrence_count=len(top_candidates),
            nearest_candidates=_build_nearest_candidates(),
            old=old,
            sanitized=sanitized,
            best_ratio=max_ratio,
        )

    return _not_found_replacement(
        best_ratio=best_ratio,
        nearest_candidates=_build_nearest_candidates(),
        old=old,
        sanitized=sanitized,
    )


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
