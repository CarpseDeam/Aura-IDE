from __future__ import annotations

import hashlib
import json
from typing import Any

from aura.conversation.path_utils import normalize_worker_path as _normalize_worker_path


def edit_shape_signature(name: str, args: dict[str, Any]) -> str:
    path = _normalize_worker_path(str(args.get("path", "")))
    if name == "edit_file":
        raw = str(args.get("old_str", ""))
        marker = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    elif name == "edit_symbol":
        marker = "|".join(
            str(args.get(key, ""))
            for key in ("symbol_type", "class_name", "symbol_name")
        )
    elif name == "edit_line_range":
        marker = f"{args.get('start_line')}:{args.get('end_line')}"
    elif name == "apply_edit_transaction":
        marker = transaction_shape_marker(args)
    elif name == "patch_file":
        marker = patch_shape_marker(args)
    else:
        marker = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return json.dumps({"tool": name, "path": path, "shape": marker}, sort_keys=True)


def patch_shape_marker(args: dict[str, Any]) -> dict[str, Any]:
    edits = args.get("edits")
    edit_markers: list[dict[str, Any]] = []
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                edit_markers.append({"invalid": True})
                continue
            marker: dict[str, Any] = {
                "allow_multiple": bool(edit.get("allow_multiple", False)),
                "has_occurrence": "occurrence" in edit,
            }
            if "occurrence" in edit:
                marker["occurrence"] = edit.get("occurrence")
            for key in ("old", "new"):
                value = edit.get(key)
                if isinstance(value, str):
                    marker[f"{key}_hash"] = hashlib.sha256(
                        value.encode("utf-8", errors="replace")
                    ).hexdigest()
            edit_markers.append(marker)
    return {
        "expected_file_hash": args.get("expected_file_hash"),
        "edits": edit_markers,
    }


def parse_patch_shape(shape: str) -> dict[str, Any]:
    try:
        parsed = json.loads(shape)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def shape_digest(shape: str) -> str:
    return hashlib.sha256(shape.encode("utf-8", errors="replace")).hexdigest()[:16]


def transaction_shape_marker(args: dict[str, Any]) -> list[dict[str, Any]]:
    operations = args.get("operations")
    if not isinstance(operations, list):
        return []
    markers: list[dict[str, Any]] = []
    for op in operations:
        if not isinstance(op, dict):
            markers.append({"op": "invalid"})
            continue
        kind = str(op.get("op") or op.get("type") or "")
        marker: dict[str, Any] = {
            "op": kind,
            "symbol_type": op.get("symbol_type"),
            "symbol_name": op.get("symbol_name")
            or op.get("function_name")
            or op.get("method_name")
            or op.get("name"),
            "class_name": op.get("class_name"),
            "occurrence": op.get("occurrence"),
            "allow_multiple": op.get("allow_multiple"),
        }
        for source_key, marker_key in (
            ("old", "old_hash"),
            ("new", "new_hash"),
            ("text", "text_hash"),
            ("new_definition", "new_definition_hash"),
            ("content", "content_hash"),
            ("start_marker", "start_marker_hash"),
            ("end_marker", "end_marker_hash"),
        ):
            value = op.get(source_key)
            if isinstance(value, str):
                marker[marker_key] = hashlib.sha256(
                    value.encode("utf-8", errors="replace")
                ).hexdigest()
        markers.append({k: v for k, v in marker.items() if v not in (None, "")})
    return markers


def has_replace_text_once_operation(args: dict[str, Any]) -> bool:
    operations = args.get("operations")
    if not isinstance(operations, list):
        return False
    return any(
        isinstance(op, dict)
        and str(op.get("op") or op.get("type") or "") == "replace_text_once"
        for op in operations
    )


def tool_path(name: str, args: dict[str, Any], parsed: Any = None) -> str:
    if isinstance(parsed, dict):
        value = parsed.get("path") or parsed.get("rel_path")
        if isinstance(value, str) and value:
            return value
    value = args.get("path")
    return str(value) if value is not None else ""
