"""Structured production outcome tool schemas."""
from __future__ import annotations

from typing import Any

REPORT_BLOCKER_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_blocker",
        "description": (
            "Report that the implementation you scoped cannot be carried out, and "
            "end the attempt. Use this ONLY when no edit is possible — not to ask "
            "for more discovery, not to restate a plan, and not instead of a write "
            "you are able to make. Performs no mutation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "blocker": {
                    "type": "string",
                    "description": (
                        "One concrete sentence naming what prevents the edit."
                    ),
                },
                "needed": {
                    "type": "string",
                    "description": (
                        "Optional: the specific thing that would unblock it."
                    ),
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: the files the edit would have touched."
                    ),
                },
            },
            "required": ["blocker"],
        },
    },
}

REPORT_ALREADY_SATISFIED_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_already_satisfied",
        "description": (
            "Report that the requested state already exists in the repository, "
            "and end the attempt. Use this ONLY when authoritative repository "
            "evidence you inspected this turn shows the change is already "
            "present and no edit is required — not to avoid a write you are "
            "able to make, and not because you simply chose not to act. "
            "Performs no mutation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "evidence": {
                    "type": "string",
                    "description": (
                        "One concrete sentence naming the authoritative "
                        "repository evidence you inspected that shows the "
                        "requested state already exists (file, symbol, search "
                        "match, or command result)."
                    ),
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: the files whose requested state is already present."
                    ),
                },
            },
            "required": ["evidence"],
        },
    },
}
