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

READ_SKILL_RESOURCE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_skill_resource",
        "description": (
            "Read one supporting resource file (references, scripts, or assets) that ships "
            "alongside an active skill's SKILL.md. A skill is active either because the user "
            "explicitly selected it for this turn or because load_skills activated it. Only "
            "that skill's own frozen directory is reachable, never a sibling or unrelated "
            "skill. This is read-only and never executes anything: a script file is read as "
            "text, never run."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "The active content-derived candidate skill_id. For an explicitly "
                        "selected skill, use the id displayed in the Explicitly Selected "
                        "Skills section. For an automatic skill, use the id returned by "
                        "load_skills."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Path to the resource, relative to the skill's own directory, "
                        "e.g. 'references/api.md' or 'scripts/setup.py'."
                    ),
                },
            },
            "required": ["skill_id", "path"],
            "additionalProperties": False,
        },
    },
}
