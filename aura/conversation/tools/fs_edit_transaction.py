"""Deterministic high-level edit transactions for existing files."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from aura.ast_utils import parse_python_ast
from aura.conversation.tools.fs_read import read_file_snapshot
from aura.conversation.tools.fs_write import (
    _failure_payload,
    _rel_path,
    expected_file_hash_matches,
    replace_line_range,
)


_SYMBOL_NODE_TYPES = {
    "function": (ast.FunctionDef, ast.AsyncFunctionDef),
    "method": (ast.FunctionDef, ast.AsyncFunctionDef),
    "class": (ast.ClassDef,),
}


def _first_string(op: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = op.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _operation_symbol_name(kind: str, op: dict[str, Any]) -> str | None:
    if kind == "replace_function":
        return _first_string(op, "symbol_name", "function_name", "name")
    if kind == "replace_method":
        return _first_string(op, "symbol_name", "method_name", "name")
    if kind == "replace_class":
        return _first_string(op, "symbol_name", "class_name", "name")
    if kind == "insert_after_symbol":
        symbol_type = str(op.get("symbol_type") or "")
        if symbol_type == "function":
            return _first_string(op, "symbol_name", "function_name", "name")
        if symbol_type == "method":
            return _first_string(op, "symbol_name", "method_name", "name")
        if symbol_type == "class":
            return _first_string(op, "symbol_name", "class_name", "name")
        return _first_string(op, "symbol_name", "function_name", "method_name", "class_name", "name")
    return _first_string(op, "symbol_name", "name")


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def _normalize_newlines(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def _preview_text(text: str, limit: int = 180) -> str:
    compact = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _safe_operation_payload(op: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in op.items():
        if isinstance(value, str):
            safe[key] = _preview_text(value)
        elif isinstance(value, (int, bool)) or value is None:
            safe[key] = value
        else:
            safe[key] = str(value)[:80]
    return safe


def _line_context(text: str, start: int, end: int, limit: int = 2) -> str:
    line_start = text.count("\n", 0, start)
    line_end = text.count("\n", 0, end)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    before = max(0, line_start - limit)
    after = min(len(lines), line_end + limit + 1)
    return "\n".join(lines[before:after])


def _candidate_payload(text: str, spans: list[tuple[int, int]], limit: int = 3) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for start, end in spans[:limit]:
        candidates.append(
            {
                "start": start,
                "end": end,
                "context": _preview_text(_line_context(text, start, end)),
            }
        )
    return candidates


def _exact_text_failure(
    *,
    error: str,
    reason: str,
    candidate_count: int = 0,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ambiguous = reason == "ambiguous"
    not_found = reason in {"not_found", "stale"}
    return {
        "failure_class": (
            "edit_transaction_ambiguous_symbol"
            if ambiguous
            else "edit_transaction_not_applicable"
        ),
        "error": error,
        "reason": reason,
        "stale": reason == "stale",
        "ambiguous": ambiguous,
        "not_found": not_found,
        "candidate_count": candidate_count,
        "occurrence_count": candidate_count,
        "candidates": candidates or [],
        "suggested_next_action": (
            "Provide occurrence to target one match, set allow_multiple true "
            "when replacing all matches is intended, or make the old/context block unique."
            if ambiguous
            else "Re-read the file and submit one corrected patch_file."
        ),
    }


def _find_all_spans(text: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    if not needle:
        return spans
    start = text.find(needle)
    while start >= 0:
        spans.append((start, start + len(needle)))
        start = text.find(needle, start + len(needle))
    return spans


def _replace_span(text: str, start: int, end: int, new: str) -> str:
    return text[:start] + new + text[end:]


def _line_window_spans(text: str, old: str, *, trim: bool) -> list[tuple[int, int]]:
    lines = text.splitlines(keepends=True)
    old_lines = old.splitlines()
    if not old_lines or len(old_lines) > len(lines):
        return []
    normalized_old = [line.strip() for line in old_lines] if trim else old_lines
    spans: list[tuple[int, int]] = []
    offsets: list[int] = []
    current = 0
    for line in lines:
        offsets.append(current)
        current += len(line)
    offsets.append(current)
    file_lines = [line.rstrip("\r\n") for line in lines]
    for index in range(len(file_lines) - len(old_lines) + 1):
        window = file_lines[index:index + len(old_lines)]
        normalized_window = [line.strip() for line in window] if trim else window
        if normalized_window == normalized_old:
            spans.append((offsets[index], offsets[index + len(old_lines)]))
    return spans


def _context_spans(text: str, before: str | None, after: str | None, newline: str) -> list[tuple[int, int]]:
    before = _normalize_newlines(before or "", newline)
    after = _normalize_newlines(after or "", newline)
    if not before and not after:
        return []
    if before and len(before.strip()) < 8:
        return []
    if after and len(after.strip()) < 8:
        return []

    spans: list[tuple[int, int]] = []
    if before and after:
        search_from = 0
        while True:
            before_start = text.find(before, search_from)
            if before_start < 0:
                break
            start = before_start + len(before)
            end = text.find(after, start)
            if end >= 0:
                spans.append((start, end))
            search_from = before_start + len(before)
        return spans
    if before:
        for before_start, before_end in _find_all_spans(text, before):
            line_end = text.find(newline, before_end)
            end = len(text) if line_end < 0 else line_end + len(newline)
            spans.append((before_end, end))
        return spans
    for after_start, _after_end in _find_all_spans(text, after):
        line_start = text.rfind(newline, 0, after_start)
        start = 0 if line_start < 0 else line_start + len(newline)
        spans.append((start, after_start))
    return spans


def _resolve_exact_text_replacement(
    proposed: str,
    *,
    old: str,
    new: str,
    newline: str,
    occurrence: int | None = None,
    allow_multiple: bool = False,
    before: str | None = None,
    after: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(old, str) or not isinstance(new, str) or old == "":
        return False, proposed, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": "replace_text_once requires non-empty string old and string new",
            "reason": "invalid",
        }

    raw_count = proposed.count(old)
    if occurrence is not None:
        if raw_count and 1 <= occurrence <= raw_count:
            return True, _replace_nth_occurrence(proposed, old, new, occurrence), {"match_tier": "exact"}
        if raw_count:
            return False, proposed, {
                "failure_class": "edit_transaction_invalid_operation",
                "error": (
                    "replace_text_once occurrence must be between 1 and "
                    f"occurrence_count ({raw_count})"
                ),
                "reason": "invalid",
                "candidate_count": raw_count,
                "occurrence_count": raw_count,
                "candidates": _candidate_payload(proposed, _find_all_spans(proposed, old)),
            }
    elif raw_count == 1:
        return True, proposed.replace(old, new, 1), {"match_tier": "exact"}
    elif raw_count > 1:
        if allow_multiple:
            return True, proposed.replace(old, new), {
                "match_tier": "exact",
                "candidate_count": raw_count,
            }
        return False, proposed, _exact_text_failure(
            error=(
                "replace_text_once old text is ambiguous; provide a 1-based "
                "occurrence or set allow_multiple true to replace every occurrence"
            ),
            reason="ambiguous",
            candidate_count=raw_count,
            candidates=_candidate_payload(proposed, _find_all_spans(proposed, old)),
        )

    normalized_old = _normalize_newlines(old, newline)
    normalized_new = _normalize_newlines(new, newline)
    if normalized_old != old:
        normalized_count = proposed.count(normalized_old)
        if occurrence is not None:
            if normalized_count and 1 <= occurrence <= normalized_count:
                return True, _replace_nth_occurrence(proposed, normalized_old, normalized_new, occurrence), {
                    "match_tier": "newline_normalized",
                    "candidate_count": normalized_count,
                }
        elif normalized_count == 1:
            return True, proposed.replace(normalized_old, normalized_new, 1), {
                "match_tier": "newline_normalized",
                "candidate_count": normalized_count,
            }
        elif normalized_count > 1 and allow_multiple:
            return True, proposed.replace(normalized_old, normalized_new), {
                "match_tier": "newline_normalized",
                "candidate_count": normalized_count,
            }
        elif normalized_count > 1:
            return False, proposed, _exact_text_failure(
                error="replace_text_once newline-normalized old text is ambiguous.",
                reason="ambiguous",
                candidate_count=normalized_count,
                candidates=_candidate_payload(proposed, _find_all_spans(proposed, normalized_old)),
            )

    trimmed_spans = _line_window_spans(proposed, normalized_old, trim=True)
    if occurrence is not None:
        if trimmed_spans and 1 <= occurrence <= len(trimmed_spans):
            start, end = trimmed_spans[occurrence - 1]
            return True, _replace_span(proposed, start, end, normalized_new), {
                "match_tier": "trimmed_whitespace",
                "candidate_count": len(trimmed_spans),
            }
    elif len(trimmed_spans) == 1:
        start, end = trimmed_spans[0]
        return True, _replace_span(proposed, start, end, normalized_new), {
            "match_tier": "trimmed_whitespace",
            "candidate_count": 1,
        }
    elif len(trimmed_spans) > 1 and allow_multiple:
        updated = proposed
        for start, end in reversed(trimmed_spans):
            updated = _replace_span(updated, start, end, normalized_new)
        return True, updated, {
            "match_tier": "trimmed_whitespace",
            "candidate_count": len(trimmed_spans),
        }
    elif len(trimmed_spans) > 1:
        return False, proposed, _exact_text_failure(
            error="replace_text_once trimmed-whitespace old text is ambiguous.",
            reason="ambiguous",
            candidate_count=len(trimmed_spans),
            candidates=_candidate_payload(proposed, trimmed_spans),
        )

    context_matches = _context_spans(proposed, before, after, newline)
    if occurrence is not None:
        if context_matches and 1 <= occurrence <= len(context_matches):
            start, end = context_matches[occurrence - 1]
            return True, _replace_span(proposed, start, end, normalized_new), {
                "match_tier": "surrounding_context",
                "candidate_count": len(context_matches),
            }
    elif len(context_matches) == 1:
        start, end = context_matches[0]
        return True, _replace_span(proposed, start, end, normalized_new), {
            "match_tier": "surrounding_context",
            "candidate_count": 1,
        }
    elif len(context_matches) > 1:
        return False, proposed, _exact_text_failure(
            error="replace_text_once surrounding context is ambiguous.",
            reason="ambiguous",
            candidate_count=len(context_matches),
            candidates=_candidate_payload(proposed, context_matches),
        )

    return False, proposed, _exact_text_failure(
        error="replace_text_once old text was not found.",
        reason="not_found",
        candidate_count=0,
    )


def _available_symbols(tree: ast.AST) -> dict[str, list[str]]:
    available: dict[str, list[str]] = {"functions": [], "classes": [], "methods": []}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            available["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            available["classes"].append(node.name)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    available["methods"].append(f"{node.name}.{child.name}")
    return available


def _node_range(node: ast.AST) -> tuple[int, int]:
    start = node.lineno - 1
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        start = decorators[0].lineno - 1
    return start, int(node.end_lineno)


def _find_symbol(
    source: str,
    *,
    symbol_type: str,
    symbol_name: str,
    class_name: str | None = None,
    filename: str,
) -> tuple[int, int, dict[str, Any]]:
    tree = parse_python_ast(source, filename=filename)
    available = _available_symbols(tree)
    if symbol_type == "method" and "." in symbol_name:
        qualified_class, qualified_method = symbol_name.rsplit(".", 1)
        if qualified_class and qualified_method:
            class_name = qualified_class
            symbol_name = qualified_method
    effective_type = "method" if class_name and symbol_type == "function" else symbol_type
    if effective_type not in _SYMBOL_NODE_TYPES:
        return -1, -1, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": f"unsupported symbol_type: {symbol_type}",
            "available_symbols": available,
        }

    if effective_type == "method":
        if not class_name:
            method_matches = _find_unqualified_methods(tree, symbol_name)
            if not method_matches:
                return -1, -1, {
                    "failure_class": "edit_transaction_symbol_not_found",
                    "error": f"Method '{symbol_name}' not found",
                    "available_symbols": available,
                }
            if len(method_matches) > 1:
                return -1, -1, {
                    "failure_class": "edit_transaction_ambiguous_symbol",
                    "error": f"Method '{symbol_name}' is ambiguous",
                    "available_symbols": available,
                    "candidates": [candidate for candidate, _node in method_matches],
                }
            start, end = _node_range(method_matches[0][1])
            return start, end, {"available_symbols": available}
        classes = [
            node for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        if not classes:
            return -1, -1, {
                "failure_class": "edit_transaction_symbol_not_found",
                "error": f"Class '{class_name}' not found",
                "available_symbols": available,
            }
        if len(classes) > 1:
            return -1, -1, {
                "failure_class": "edit_transaction_ambiguous_symbol",
                "error": f"Class '{class_name}' is ambiguous",
                "available_symbols": available,
            }
        matches = [
            child for child in ast.iter_child_nodes(classes[0])
            if isinstance(child, _SYMBOL_NODE_TYPES["method"]) and child.name == symbol_name
        ]
    else:
        matches = [
            node for node in ast.iter_child_nodes(tree)
            if isinstance(node, _SYMBOL_NODE_TYPES[effective_type]) and node.name == symbol_name
        ]

    if not matches:
        return -1, -1, {
            "failure_class": "edit_transaction_symbol_not_found",
            "error": f"{effective_type.title()} '{symbol_name}' not found",
            "available_symbols": available,
        }
    if len(matches) > 1:
        return -1, -1, {
            "failure_class": "edit_transaction_ambiguous_symbol",
            "error": f"{effective_type.title()} '{symbol_name}' is ambiguous",
            "available_symbols": available,
        }
    start, end = _node_range(matches[0])
    return start, end, {"available_symbols": available}


def _find_unqualified_methods(
    tree: ast.AST,
    method_name: str,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    matches: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SYMBOL_NODE_TYPES["method"]) and child.name == method_name:
                matches.append((f"{node.name}.{child.name}", child))
    return matches


def _indent_like_existing(original_block: str, replacement: str) -> str:
    first_orig_line = original_block.splitlines()[0] if original_block.splitlines() else ""
    orig_indent = first_orig_line[: len(first_orig_line) - len(first_orig_line.lstrip())]
    if not orig_indent:
        return replacement
    first_new_line = replacement.splitlines()[0] if replacement.splitlines() else ""
    if first_new_line.startswith(orig_indent):
        return replacement
    return "\n".join((orig_indent + line) if line else "" for line in replacement.split("\n"))


def _replace_symbol(
    proposed: str,
    *,
    target: Path,
    symbol_type: str,
    symbol_name: str,
    new_definition: str,
    class_name: str | None,
    newline: str,
) -> tuple[bool, str, dict[str, Any]]:
    try:
        start, end, info = _find_symbol(
            proposed,
            symbol_type=symbol_type,
            symbol_name=symbol_name,
            class_name=class_name,
            filename=str(target),
        )
    except SyntaxError as exc:
        return False, proposed, {
            "failure_class": "edit_transaction_not_applicable",
            "error": f"Current Python is not parseable: {exc}",
        }
    if start < 0:
        return False, proposed, info

    lines = proposed.splitlines(keepends=True)
    old_block = "".join(lines[start:end])
    replacement = _normalize_newlines(new_definition, "\n")
    replacement = _indent_like_existing(old_block, replacement)
    replacement = _normalize_newlines(replacement, newline)
    if old_block.endswith(("\n", "\r")) and not replacement.endswith(("\n", "\r")):
        replacement += newline
    if not old_block.endswith(("\n", "\r")) and replacement.endswith(("\n", "\r")):
        replacement = replacement.rstrip("\r\n")
    return True, replace_line_range(proposed, lines, start, end, replacement), {}


def _insert_after_symbol(
    proposed: str,
    *,
    target: Path,
    symbol_type: str,
    symbol_name: str,
    class_name: str | None,
    content: str,
    newline: str,
) -> tuple[bool, str, dict[str, Any]]:
    try:
        start, end, info = _find_symbol(
            proposed,
            symbol_type=symbol_type,
            symbol_name=symbol_name,
            class_name=class_name,
            filename=str(target),
        )
    except SyntaxError as exc:
        return False, proposed, {
            "failure_class": "edit_transaction_not_applicable",
            "error": f"Current Python is not parseable: {exc}",
        }
    if start < 0:
        return False, proposed, info

    lines = proposed.splitlines(keepends=True)
    insertion = _normalize_newlines(content, newline)
    if end > 0 and not lines[end - 1].endswith(("\n", "\r")):
        insertion = newline + insertion
    if insertion and not insertion.endswith(("\n", "\r")):
        insertion += newline
    return True, replace_line_range(proposed, lines, end, end, insertion), {}


def _replace_text_once(
    proposed: str,
    *,
    old: str,
    new: str,
    newline: str,
    occurrence: int | None = None,
    allow_multiple: bool = False,
    before: str | None = None,
    after: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    return _resolve_exact_text_replacement(
        proposed,
        old=old,
        new=new,
        newline=newline,
        occurrence=occurrence,
        allow_multiple=allow_multiple,
        before=before,
        after=after,
    )


def _replace_nth_occurrence(text: str, old: str, new: str, occurrence: int) -> str:
    start = -1
    search_from = 0
    for _ in range(occurrence):
        start = text.find(old, search_from)
        if start < 0:
            return text
        search_from = start + len(old)
    return text[:start] + new + text[start + len(old):]


def _clean_removed_newlines(text: str, newline: str) -> str:
    doubled = newline * 3
    while doubled in text:
        text = text.replace(doubled, newline * 2)
    return text


def _remove_text_once(
    proposed: str,
    *,
    text: str,
    newline: str,
    occurrence: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(text, str) or text == "":
        return False, proposed, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": "remove_text_once requires non-empty string text",
            "reason": "invalid",
        }
    ok, updated, info = _resolve_exact_text_replacement(
        proposed,
        old=text,
        new="",
        newline=newline,
        occurrence=occurrence,
        allow_multiple=False,
    )
    if ok:
        return True, _clean_removed_newlines(updated, newline), info
    if info.get("error", "").startswith("replace_text_once"):
        info["error"] = str(info["error"]).replace("replace_text_once", "remove_text_once", 1)
    return False, proposed, info


def _remove_text_all(
    proposed: str,
    *,
    text: str,
    allow_multiple: bool,
    newline: str,
) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(text, str) or text == "":
        return False, proposed, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": "remove_text_all requires non-empty string text",
        }
    if allow_multiple is not True:
        return False, proposed, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": "remove_text_all requires allow_multiple=true",
        }
    text = _normalize_newlines(text, newline)
    count = proposed.count(text)
    if count == 0:
        return False, proposed, {
            "failure_class": "edit_transaction_not_applicable",
            "error": "remove_text_all text was not found",
            "occurrence_count": 0,
        }
    return True, _clean_removed_newlines(proposed.replace(text, ""), newline), {"occurrence_count": count}


def _remove_between_markers(
    proposed: str,
    *,
    start_marker: str,
    end_marker: str,
    newline: str,
) -> tuple[bool, str, dict[str, Any]]:
    if not start_marker or not end_marker:
        return False, proposed, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": "remove_between_markers requires start_marker and end_marker",
        }
    start_marker = _normalize_newlines(start_marker, newline)
    end_marker = _normalize_newlines(end_marker, newline)
    start_count = proposed.count(start_marker)
    end_count = proposed.count(end_marker)
    if start_count != 1 or end_count != 1:
        return False, proposed, {
            "failure_class": "edit_transaction_ambiguous_symbol",
            "error": "remove_between_markers requires exact unique start and end markers",
            "start_marker_count": start_count,
            "end_marker_count": end_count,
        }
    start = proposed.find(start_marker)
    end = proposed.find(end_marker, start + len(start_marker))
    if end < 0:
        return False, proposed, {
            "failure_class": "edit_transaction_not_applicable",
            "error": "end_marker does not occur after start_marker",
        }
    end += len(end_marker)
    return True, _clean_removed_newlines(proposed[:start] + proposed[end:], newline), {}


def propose_edit_transaction(
    workspace_root: Path,
    target: Path,
    operations: list[dict[str, Any]],
    expected_file_hash: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Propose an atomic structured edit transaction for one existing file."""
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
        if not expected_file_hash_matches(original, current_hash, expected_file_hash):
            return _failure_payload(
                workspace_root,
                target,
                "File content did not match expected_file_hash.",
                "edit_transaction_hash_mismatch",
            )
    if not operations:
        return _failure_payload(
            workspace_root,
            target,
            "operations must contain at least one operation",
            "edit_transaction_invalid_operation",
            old_content=original,
            new_content="",
            is_new_file=False,
        )

    newline = _dominant_newline(original)
    proposed = original
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            return _failure_payload(
                workspace_root,
                target,
                "each operation must be an object",
                "edit_transaction_invalid_operation",
                operation_index=index,
            )
        kind = op.get("op") or op.get("type")
        if kind in {"replace_function", "replace_method", "replace_class"}:
            symbol_type = {
                "replace_function": "function",
                "replace_method": "method",
                "replace_class": "class",
            }[str(kind)]
            symbol_name = _operation_symbol_name(str(kind), op)
            new_definition = op.get("new_definition")
            class_name = op.get("class_name")
            if not isinstance(symbol_name, str) or not isinstance(new_definition, str):
                return _failure_payload(
                    workspace_root,
                    target,
                    f"{kind} requires symbol_name and new_definition strings",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                )
            ok, proposed, failure = _replace_symbol(
                proposed,
                target=target,
                symbol_type=symbol_type,
                symbol_name=symbol_name,
                new_definition=new_definition,
                class_name=str(class_name) if class_name is not None else None,
                newline=newline,
            )
        elif kind == "insert_after_symbol":
            symbol_type = op.get("symbol_type")
            symbol_name = _operation_symbol_name(str(kind), op)
            content = op.get("content")
            class_name = op.get("class_name")
            if not isinstance(symbol_type, str) or not isinstance(symbol_name, str) or not isinstance(content, str):
                return _failure_payload(
                    workspace_root,
                    target,
                    "insert_after_symbol requires symbol_type, symbol_name, and content strings",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                )
            ok, proposed, failure = _insert_after_symbol(
                proposed,
                target=target,
                symbol_type=symbol_type,
                symbol_name=symbol_name,
                class_name=str(class_name) if class_name is not None else None,
                content=content,
                newline=newline,
            )
        elif kind == "replace_text_once":
            occurrence = op.get("occurrence")
            if occurrence is not None and (
                not isinstance(occurrence, int) or isinstance(occurrence, bool)
            ):
                return _failure_payload(
                    workspace_root,
                    target,
                    "replace_text_once occurrence must be an integer",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                    old_content=original,
                    new_content="",
                    is_new_file=False,
                )
            allow_multiple = op.get("allow_multiple", False)
            if not isinstance(allow_multiple, bool):
                return _failure_payload(
                    workspace_root,
                    target,
                    "replace_text_once allow_multiple must be a boolean",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                    old_content=original,
                    new_content="",
                    is_new_file=False,
                )
            ok, proposed, failure = _replace_text_once(
                proposed,
                old=op.get("old"),
                new=op.get("new"),
                newline=newline,
                occurrence=occurrence,
                allow_multiple=allow_multiple,
                before=op.get("before") if isinstance(op.get("before"), str) else None,
                after=op.get("after") if isinstance(op.get("after"), str) else None,
            )
        elif kind == "remove_text_once":
            occurrence = op.get("occurrence")
            if occurrence is not None and (
                not isinstance(occurrence, int) or isinstance(occurrence, bool)
            ):
                return _failure_payload(
                    workspace_root,
                    target,
                    "remove_text_once occurrence must be an integer",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                    old_content=original,
                    new_content="",
                    is_new_file=False,
                )
            ok, proposed, failure = _remove_text_once(
                proposed,
                text=op.get("text", op.get("old")),
                newline=newline,
                occurrence=occurrence,
            )
        elif kind == "remove_text_all":
            allow_multiple = op.get("allow_multiple", False)
            if not isinstance(allow_multiple, bool):
                return _failure_payload(
                    workspace_root,
                    target,
                    "remove_text_all allow_multiple must be a boolean",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                    old_content=original,
                    new_content="",
                    is_new_file=False,
                )
            ok, proposed, failure = _remove_text_all(
                proposed,
                text=op.get("text", op.get("old")),
                allow_multiple=allow_multiple,
                newline=newline,
            )
        elif kind == "remove_between_markers":
            start_marker = op.get("start_marker")
            end_marker = op.get("end_marker")
            if not isinstance(start_marker, str) or not isinstance(end_marker, str):
                return _failure_payload(
                    workspace_root,
                    target,
                    "remove_between_markers requires string start_marker and end_marker",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                    old_content=original,
                    new_content="",
                    is_new_file=False,
                )
            ok, proposed, failure = _remove_between_markers(
                proposed,
                start_marker=start_marker,
                end_marker=end_marker,
                newline=newline,
            )
        else:
            return _failure_payload(
                workspace_root,
                target,
                f"unsupported edit transaction operation: {kind}",
                "edit_transaction_invalid_operation",
                operation_index=index,
            )
        if not ok:
            failure.setdefault("failure_class", "edit_transaction_not_applicable")
            failure.setdefault("error", "edit transaction operation could not be applied")
            failure.setdefault("reason", "unknown")
            failure.setdefault("candidate_count", 0)
            return _failure_payload(
                workspace_root,
                target,
                str(failure.get("error")),
                str(failure.get("failure_class")),
                operation_index=index,
                failed_operation=_safe_operation_payload(op),
                reason=str(failure.get("reason") or "unknown"),
                stale=bool(failure.get("stale")),
                ambiguous=bool(failure.get("ambiguous")),
                not_found=bool(failure.get("not_found")),
                candidate_count=int(failure.get("candidate_count") or failure.get("occurrence_count") or 0),
                occurrence_count=int(failure.get("occurrence_count") or failure.get("candidate_count") or 0),
                old_content=original,
                new_content="",
                is_new_file=False,
                **{
                    k: v for k, v in failure.items()
                    if k not in {
                        "error",
                        "failure_class",
                        "reason",
                        "stale",
                        "ambiguous",
                        "not_found",
                        "candidate_count",
                        "occurrence_count",
                    }
                },
            )

    if target.suffix == ".py":
        try:
            parse_python_ast(proposed, filename=str(target))
        except SyntaxError as exc:
            return _failure_payload(
                workspace_root,
                target,
                f"transaction produces invalid Python: {exc}",
                "edit_transaction_invalid_syntax",
                old_content=original,
                new_content="",
                is_new_file=False,
            )

    return {
        "ok": True,
        "path": rel,
        "rel_path": rel,
        "old_content": original,
        "new_content": proposed,
        "is_new_file": False,
        "operation_count": len(operations),
        "description": description or "",
    }
