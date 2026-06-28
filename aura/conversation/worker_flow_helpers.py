"""Pure helper/config logic extracted from worker_flow.py.

Holds classification constants, regex patterns, and pure helper functions
used by WorkerFlowHarness. No harness state machine or steering logic here.
"""

from __future__ import annotations

import json
import re
from typing import Any


# ── Tool classification sets ──────────────────────────────────────────

BROAD_ORIENTATION_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "read_files",
        "read_file_outline",
        "list_directory",
        "glob",
        "grep_search",
        "search_codebase",
    }
)

TARGETED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file_range",
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
    }
)

WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "patch_file", "delete_file"})
VALIDATION_TOOLS: frozenset[str] = frozenset({"run_terminal_command", "run_and_watch"})


# ── Regex patterns (helper/classification only) ──────────────────────

_PATH_RE = re.compile(
    r"(?<![\w./\\-])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+."
    r"(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml|css|html|go|rs|java|cs|cpp|hpp|h|c|sh|ps1|txt)"
    r"\b|\b[A-Za-z_][\w.-]*\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml)\b"
)

_PLANNING_MARKER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\blet\s+me\b", re.IGNORECASE),
    re.compile(r"\bnow\s+i\s+have\b", re.IGNORECASE),
    re.compile(r"\bfull\s+picture\b", re.IGNORECASE),
    re.compile(r"\bcomplete\s+picture\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+plan\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+verify\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+check\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+read\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+think\b", re.IGNORECASE),
    re.compile(r"\bi\s+need\s+to\s+be\s+careful\b", re.IGNORECASE),
    re.compile(r"\bi\s+should\s+be\s+careful\b", re.IGNORECASE),
    re.compile(r"\bactually\b", re.IGNORECASE),
    re.compile(r"\bwait\b", re.IGNORECASE),
)

_FULL_OR_COMPLETE_PICTURE_RE = re.compile(
    r"\b(?:full|complete)\s+picture\b",
    re.IGNORECASE,
)

_PICTURE_FOLLOWUP_RE = re.compile(
    r"\b(?:"
    r"let\s+me\s+(?:read|verify|check|plan|think|analy[sz]e)|"
    r"i\s+(?:need|should)\s+(?:read|verify|check|plan|think|analy[sz]e)|"
    r"check\s+(?:tests?|imports?|files?|usages?|references?)|"
    r"plan\s+(?:helpers?|module|hunks?|edits?|patches?)"
    r")\b",
    re.IGNORECASE,
)

_PLAN_SAYS_RE = re.compile(r"\bplan\s+says\b", re.IGNORECASE)
_HUNK_MECHANICS_RE = re.compile(r"\bhunks?\b", re.IGNORECASE)

_PATCH_PLAN_MECHANICS_RE = re.compile(
    r"\b(?:plan\s+(?:helpers?|module|hunks?|edits?|patches?)|"
    r"patch\s+hunks?|hunk\s+plan)\b",
    re.IGNORECASE,
)

_IMPORT_HELPER_MECHANICS_RE = re.compile(
    r"\b(?:remove|add)\s+(?:imports?|regex(?:es)?|helpers?|functions?|constants?)\b",
    re.IGNORECASE,
)

_INVENTORY_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]|\d+[.)])\s+.*\b"
    r"(?:file|function|helper|import|regex|constant|class|hunk|\.py)\b"
)


# ── Pure helper functions ────────────────────────────────────────────


def _assistant_text(full_message: dict[str, Any] | str | None) -> str:
    if full_message is None:
        return ""
    if isinstance(full_message, str):
        return full_message
    if not isinstance(full_message, dict):
        return ""
    return _content_text(full_message.get("content")) + "\n" + _content_text(
        full_message.get("reasoning_content")
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    return ""


def _planning_marker_count(text: str) -> int:
    return sum(len(pattern.findall(text)) for pattern in _PLANNING_MARKER_RES)


def _inventory_restatement_marker_count(text: str) -> int:
    count = 0
    count += len(_PLAN_SAYS_RE.findall(text))
    count += len(_HUNK_MECHANICS_RE.findall(text))
    count += len(_PATCH_PLAN_MECHANICS_RE.findall(text))
    count += len(_IMPORT_HELPER_MECHANICS_RE.findall(text))
    inventory_lines = len(_INVENTORY_LINE_RE.findall(text))
    if inventory_lines >= 3:
        count += inventory_lines
    return count


def _has_full_picture_plus_followup(text: str) -> bool:
    return bool(
        _FULL_OR_COMPLETE_PICTURE_RE.search(text)
        and _PICTURE_FOLLOWUP_RE.search(text)
    )


def _tool_call_name_args(tool_call: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return "", {}
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return "", {}
    name = str(function.get("name") or "")
    raw_args = function.get("arguments") or "{}"
    if isinstance(raw_args, dict):
        return name, raw_args
    if not isinstance(raw_args, str):
        return name, {}
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return name, {}
    return name, parsed if isinstance(parsed, dict) else {}


def _path_mentions(text: str) -> list[str]:
    return [_normalize_path(match.group(0)) for match in _PATH_RE.finditer(text)]


def _tool_paths(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> list[str]:
    paths: list[str] = []
    for key in ("path", "rel_path", "file", "target"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value)
    raw_paths = args.get("paths")
    if isinstance(raw_paths, list):
        paths.extend(str(path) for path in raw_paths if str(path).strip())
    if name == "glob" and isinstance(args.get("pattern"), str):
        paths.append(f"glob:{args['pattern']}")
    if name in {"grep_search", "search_codebase"}:
        for key in ("path", "path_filter", "include_glob", "glob"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value)

    if payload:
        for key in ("path", "rel_path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value)
        files = payload.get("files")
        if isinstance(files, dict):
            paths.extend(str(key) for key in files if str(key).strip())

    normalized = [_normalize_path(path) for path in paths if _normalize_path(path)]
    return list(dict.fromkeys(normalized))


def _normalize_path(path: str) -> str:
    normalized = str(path).strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _parse_payload(result: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str) or not result.strip():
        return {}
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_payload_items(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    if payload:
        path = _first_path(payload) or next(iter(_tool_paths(name, args)), "")
        if path:
            items.append((path, payload))
        files = payload.get("files")
        if isinstance(files, dict):
            for raw_path, item in files.items():
                if isinstance(item, dict):
                    items.append((_normalize_path(str(raw_path)), item))
    return items


def _first_path(payload: dict[str, Any]) -> str:
    for key in ("path", "rel_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_path(value)
    return ""


def _payload_is_large(
    payload: dict[str, Any], large_file_bytes: int, large_file_lines: int
) -> bool:
    file_size = _int_or_none(payload.get("file_size"))
    if file_size is not None and file_size >= large_file_bytes:
        return True
    total_lines = _int_or_none(payload.get("total_lines"))
    if total_lines is not None and total_lines >= large_file_lines:
        return True
    content = payload.get("content")
    return isinstance(content, str) and len(content) >= large_file_bytes


def _write_was_applied(
    name: str, ok: bool | None, payload: dict[str, Any]
) -> bool:
    if ok is False:
        return False
    if payload.get("applied") is True:
        return True
    if payload.get("ok") is True and name == "delete_file":
        return True
    return bool(ok and not payload)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BROAD_ORIENTATION_TOOLS",
    "TARGETED_READ_TOOLS",
    "WRITE_TOOLS",
    "VALIDATION_TOOLS",
    "_PATH_RE",
    "_PLANNING_MARKER_RES",
    "_FULL_OR_COMPLETE_PICTURE_RE",
    "_PICTURE_FOLLOWUP_RE",
    "_PLAN_SAYS_RE",
    "_HUNK_MECHANICS_RE",
    "_PATCH_PLAN_MECHANICS_RE",
    "_IMPORT_HELPER_MECHANICS_RE",
    "_INVENTORY_LINE_RE",
    "_assistant_text",
    "_content_text",
    "_planning_marker_count",
    "_inventory_restatement_marker_count",
    "_has_full_picture_plus_followup",
    "_tool_call_name_args",
    "_path_mentions",
    "_tool_paths",
    "_normalize_path",
    "_parse_payload",
    "_read_payload_items",
    "_first_path",
    "_payload_is_large",
    "_write_was_applied",
    "_int_or_none",
]
