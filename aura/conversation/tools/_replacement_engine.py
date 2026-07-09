"""Replacement matching engine: exact and line-exact apply tiers plus fuzzy diagnostics."""
from __future__ import annotations

import difflib
import re
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


def _preview_block(text: str, limit: int = 500) -> str:
    compact = text.replace("\r\n", "\n").replace("\r", "\n")
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


def _repr_lines(lines: list[str]) -> str:
    return "\n".join(repr(line) for line in lines)


def _line_context(text: str, start: int, end: int, limit: int = 2) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    start_line = _span_line_number(text, start)
    end_line = _span_line_number(text, end)
    before = max(1, start_line - limit)
    after = min(len(lines), end_line + limit)
    return _repr_lines(lines[before - 1:after])


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
                "text": _repr_lines(file_lines[idx:idx + window_len]),
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
    old = old.replace("\r\n", "\n").replace("\r", "\n")
    duplicate_hint = (
        best_ratio >= 0.999
        and len(nearest_candidates) > 1
        and len({str(candidate.get("text", "")) for candidate in nearest_candidates}) == 1
    )
    if duplicate_hint:
        error = (
            "old_str appears more than once in the file. Add more surrounding "
            "context so the old block uniquely identifies the target. Nearest "
            "matching lines follow with exact whitespace rendered."
        )
    else:
        error = (
            "old_str was not found by exact or line-exact matching. Nearest "
            "lines in the file follow with exact whitespace rendered. The old "
            "block must reproduce those lines character for character, including "
            "trailing spaces on lines that appear blank."
        )
    result: dict[str, Any] = {
        "ok": False,
        "reason": "not_found",
        "match_tier": match_tier,
        "old": old,
        "error": error,
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
    old = old.replace("\r\n", "\n").replace("\r", "\n")
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

    Exact string and exact line-window matches may apply replacements. The
    whitespace-tolerant fuzzy pass is diagnostic-only and feeds failure context.
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
        line_spans = []
        for start in line_matches:
            span_start = offsets[start]
            span_end = offsets[start + window_len]
            if (
                not _match_old.endswith("\n")
                and span_end > 0
                and _match_content[span_end - 1] == "\n"
            ):
                span_end -= 1
            line_spans.append((span_start, span_end))
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

    # ---- Diagnostic pass: whitespace-agnostic fuzzy line scoring ----
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
            if ratio > 0.5:
                all_near_matches.append((i, ratio))

    def _build_nearest_candidates() -> list[dict[str, Any]]:
        sorted_matches = sorted(all_near_matches, key=lambda x: -x[1])
        return _build_line_candidates(
            file_lines,
            [idx for idx, _ratio in sorted_matches],
            window_len,
        )

    return _not_found_replacement(
        best_ratio=best_ratio,
        nearest_candidates=_build_nearest_candidates(),
        match_tier="fuzzy",
        old=old,
        sanitized=sanitized,
    )
