"""Write/mutation tracking helpers for WorkerEventRelay.

Constants and pure functions for detecting file-mutation tools,
extracting paths from tool payloads, and computing progress-detail
metadata used by the relay's TODO progress machinery.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aura.conversation.tool_limits import WRITE_TOOLS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TERMINAL_OUTPUT_CAPTURE_CHARS = 4000
TERMINAL_OUTPUT_PREVIEW_CHARS = 200

LEGACY_EDIT_TOOLS = frozenset(
    {
        "edit_file",
        "edit_symbol",
        "edit_line_range",
        "apply_edit_transaction",
    }
)
FILE_MUTATION_TOOLS = frozenset(WRITE_TOOLS) | LEGACY_EDIT_TOOLS

READ_PROGRESS_TOOLS = frozenset(
    {
        "read_file",
        "read_files",
        "read_file_range",
        "read_file_outline",
        "grep_search",
        "search_codebase",
        "glob",
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
    }
)
VALIDATION_PROGRESS_TOOLS = frozenset({"run_terminal_command", "run_and_watch"})

DEFAULT_WRITE_ACTION_WORDS = (
    "edit",
    "update",
    "modify",
    "change",
    "refactor",
    "extract",
)
PATH_FIELDS = ("path", "rel_path", "file", "filename", "target_path")
PATH_MENTION_RE = re.compile(
    r"(?<![\w.-])[\w.@~+/\\-]+(?:\.[\w+-]{1,12})(?::\d+(?::\d+)?)?"
)

# ---------------------------------------------------------------------------
# Shared path / normalisation utilities
# ---------------------------------------------------------------------------


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().strip("`\"'.,;()[]{}<>")
    if not text:
        return ""
    text = text.replace("\\", "/")
    text = re.sub(r":\d+(?::\d+)?$", "", text)
    while text.startswith("./"):
        text = text[2:]
    return text.lower()


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


# ---------------------------------------------------------------------------
# Path extraction from tool payloads / results / extras
# ---------------------------------------------------------------------------


def _append_path_values(paths: list[str], value: Any) -> None:
    if isinstance(value, str):
        normalized = _normalize_path(value)
        if normalized:
            paths.append(normalized)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _append_path_values(paths, item)
        return
    if isinstance(value, dict):
        for field in PATH_FIELDS:
            _append_path_values(paths, value.get(field))
        for key, item in value.items():
            if isinstance(key, str) and not key.startswith("__"):
                paths.append(_normalize_path(key))
            if isinstance(item, dict):
                for field in PATH_FIELDS:
                    _append_path_values(paths, item.get(field))


def _payload_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for field in PATH_FIELDS:
        _append_path_values(paths, payload.get(field))
    _append_path_values(paths, payload.get("paths"))
    _append_path_values(paths, payload.get("files"))
    return _dedupe([path for path in paths if path])


def _extract_json_string_field(text: str, fields: tuple[str, ...]) -> str:
    for field in fields:
        match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    return ""


def _result_path(parsed: dict[str, Any], extras: dict[str, Any]) -> str:
    for source in (parsed, extras):
        for field in PATH_FIELDS:
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ---------------------------------------------------------------------------
# File-mutation tool detection
# ---------------------------------------------------------------------------


def _is_file_mutation_tool(name: str) -> bool:
    return name in FILE_MUTATION_TOOLS


def _file_mutation_was_applied(
    name: str, ok: bool, parsed: Any, extras: dict[str, Any]
) -> bool:
    if not _is_file_mutation_tool(name):
        return False
    if ok is False or not isinstance(parsed, dict):
        return False
    if parsed.get("applied") is True:
        return bool(_result_path(parsed, extras))
    if parsed.get("applied") is False:
        return False
    return parsed.get("ok") is True and bool(_result_path(parsed, extras))


# ---------------------------------------------------------------------------
# Progress-detail helpers (action words, paths for TODO overlay)
# ---------------------------------------------------------------------------


def _write_action_words(name: str, payload: dict[str, Any]) -> list[str]:
    if not _is_file_mutation_tool(name):
        return []
    if name == "delete_file" or payload.get("deleted"):
        return ["remove", "delete"]
    if payload.get("is_new_file"):
        return ["create", "add", "new", "write"]
    if name == "write_file":
        return ["create", "update", "write", "edit", "modify"]
    return list(DEFAULT_WRITE_ACTION_WORDS)


def _tool_progress_details_from_payload(
    name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "paths": _payload_paths(payload),
        "action_words": _write_action_words(name, payload),
    }


def _tool_progress_details_from_args(
    name: str, args_text: str
) -> dict[str, Any]:
    if not args_text:
        return _tool_progress_details_from_payload(name, {})
    try:
        parsed = json.loads(args_text)
    except (json.JSONDecodeError, TypeError):
        payload: dict[str, Any] = {}
        path = _extract_json_string_field(args_text, PATH_FIELDS)
        if path:
            payload["path"] = path
        return _tool_progress_details_from_payload(name, payload)
    if not isinstance(parsed, dict):
        parsed = {}
    return _tool_progress_details_from_payload(name, parsed)


def _tool_progress_details_from_result(
    name: str, parsed: Any, args_text: str
) -> dict[str, Any]:
    details = _tool_progress_details_from_payload(
        name, parsed if isinstance(parsed, dict) else {}
    )
    arg_details = _tool_progress_details_from_args(name, args_text)
    details["paths"] = _dedupe(
        [
            *details.get("paths", []),
            *arg_details.get("paths", []),
        ]
    )
    details["action_words"] = _dedupe(
        [
            *details.get("action_words", []),
            *arg_details.get("action_words", []),
        ]
    )
    return details


# ---------------------------------------------------------------------------
# Progress-phase classification
# ---------------------------------------------------------------------------


def _progress_key_for_tool(name: str) -> str:
    if name == "update_todo_list":
        return ""
    if name in READ_PROGRESS_TOOLS:
        return "inspect"
    if _is_file_mutation_tool(name):
        return "edit"
    if name in VALIDATION_PROGRESS_TOOLS:
        return "validate"
    return ""
