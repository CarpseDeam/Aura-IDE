"""Root-only operations over retained writable Agent change sets."""
from __future__ import annotations

from typing import Any

_ID_PROPERTY = {
    "type": "string",
    "minLength": 1,
    "description": "The exact change_set_id returned by delegate_agent.",
}

INSPECT_AGENT_CHANGE_SET_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "inspect_agent_change_set",
        "description": (
            "Inspect a retained writable Agent result without changing the canonical "
            "workspace. Returns its frozen base/result SHAs, changed paths, diffstat, "
            "and textual diff (with binary files identified, not decoded)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"change_set_id": _ID_PROPERTY},
            "required": ["change_set_id"],
            "additionalProperties": False,
        },
    },
}

APPLY_AGENT_CHANGE_SET_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "apply_agent_change_set",
        "description": (
            "Ask the user to approve one retained Agent change set, then apply it "
            "to the canonical workspace only when that workspace is still clean and "
            "at the exact frozen base. Never resolves conflicts or overwrites a moved "
            "or dirty primary worktree; a refusal preserves the result."
        ),
        "parameters": {
            "type": "object",
            "properties": {"change_set_id": _ID_PROPERTY},
            "required": ["change_set_id"],
            "additionalProperties": False,
        },
    },
}

DISCARD_AGENT_CHANGE_SET_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "discard_agent_change_set",
        "description": (
            "Ask the user to approve discarding one exact retained Agent change set. "
            "Deletes only its Aura-owned worktree/ref and preserves it if safe cleanup "
            "cannot complete. This never changes canonical workspace files."
        ),
        "parameters": {
            "type": "object",
            "properties": {"change_set_id": _ID_PROPERTY},
            "required": ["change_set_id"],
            "additionalProperties": False,
        },
    },
}

AGENT_CHANGE_SET_TOOL_DEFS = [
    INSPECT_AGENT_CHANGE_SET_TOOL_DEF,
    APPLY_AGENT_CHANGE_SET_TOOL_DEF,
    DISCARD_AGENT_CHANGE_SET_TOOL_DEF,
]

__all__ = [
    "AGENT_CHANGE_SET_TOOL_DEFS",
    "APPLY_AGENT_CHANGE_SET_TOOL_DEF",
    "DISCARD_AGENT_CHANGE_SET_TOOL_DEF",
    "INSPECT_AGENT_CHANGE_SET_TOOL_DEF",
]
