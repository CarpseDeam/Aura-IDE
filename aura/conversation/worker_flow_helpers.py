"""Pure helper/config logic used by WorkerFlowHarness.

Holds classification constants and pure helper functions.
No regex patterns for assistant-text surveillance remain.
"""
from __future__ import annotations

import json
from typing import Any

# ── Tool classification sets ──────────────────────────────────────────

TARGETED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file_range",
        "read_file_outline",
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
    }
)

WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "patch_file",
        "delete_file",
        "edit_godot_scene",
        "edit_godot_editor",
        "edit_godot_asset_preview",
        "install_godot_editor_bridge",
    }
)
VALIDATION_TOOLS: frozenset[str] = frozenset({"run_terminal_command", "run_and_watch"})


# ── Pure helper functions ────────────────────────────────────────────


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


def _write_was_applied(
    name: str, ok: bool | None, payload: dict[str, Any]
) -> bool:
    if ok is False:
        return False
    if payload.get("applied") is True:
        return True
    if payload.get("applied") is False:
        return False
    if payload.get("ok") is True and name in WRITE_TOOLS:
        return True
    return bool(ok and not payload)


__all__ = [
    "TARGETED_READ_TOOLS",
    "WRITE_TOOLS",
    "VALIDATION_TOOLS",
    "_tool_call_name_args",
    "_tool_paths",
    "_normalize_path",
    "_parse_payload",
    "_write_was_applied",
]
