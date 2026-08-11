"""Skill-loading tool schemas."""
from __future__ import annotations

from typing import Any

LOAD_SKILLS_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_skills",
        "description": (
            "Load the full body of a candidate skill already listed in this "
            "turn's initial skill index. Only skills in that index are loadable; "
            "the index is frozen for this request, so unrelated global skills "
            "are never available. Several ids may be loaded in one call. "
            "This is read-only and never changes any file. "
            "Returned bodies are the exact bodies the index described (each with "
            "a body hash), and re-loading an already-active skill is inert."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Stable skill ids from the current turn's skill index. "
                        "Each must be one of the ids listed in the initial "
                        "### Skills block."
                    ),
                },
            },
            "required": ["skill_ids"],
            "additionalProperties": False,
        },
    },
}
