from __future__ import annotations

import json
from typing import Any

from aura.client import ToolResult

__all__ = [
    "blocked_tool_result",
    "is_recoverable_phase_boundary",
    "parse_tool_payload",
    "record_recovery_block",
    "recovery_payload",
]


def parse_tool_payload(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None


def recovery_payload(
    *,
    path: str,
    failure_class: str,
    error: str,
    suggested_next_tool: str,
    suggested_next_action: str,
    recoverable: bool = True,
) -> dict[str, Any]:
    return {
        "ok": False,
        "path": path,
        "rel_path": path,
        "error": error,
        "failure_class": failure_class,
        "recoverable": recoverable,
        "internal_recovery_steer": True,
        "suggested_tool": suggested_next_tool,
        "suggested_next_tool": suggested_next_tool,
        "suggested_next_action": suggested_next_action,
    }


def record_recovery_block(
    payload: dict[str, Any],
    key: str,
    recovery_block_counts: dict[str, int],
) -> None:
    count = recovery_block_counts.get(key, 0) + 1
    recovery_block_counts[key] = count
    payload["repeated_blocks"] = count


def blocked_tool_result(tool_call_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    content = json.dumps(payload, ensure_ascii=False)
    return {
        "id": tool_call_id,
        "result_payload": content,
        "event": ToolResult(
            tool_call_id=tool_call_id,
            name=name,
            ok=False,
            result=content,
        ),
    }


def is_recoverable_phase_boundary(info: dict[str, Any] | None) -> bool:
    return bool(info and info.get("recoverable") and info.get("phase_boundary"))
