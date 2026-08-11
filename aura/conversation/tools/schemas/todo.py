"""Worker TODO tool schemas."""
from __future__ import annotations

from typing import Any

WORKER_TODO_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_worker_todo",
        "description": (
            "Publish the live TODO checklist as a full snapshot, replacing the previous one. "
            "Requires three to seven action-shaped rows. Exactly one item should be active unless "
            "the work is complete, in which case all items may be done. This tool is only a UI "
            "lens; it never completes, blocks, or gates the task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Full ordered TODO snapshot. Reuse each row id across updates.",
                    "minItems": 3,
                    "maxItems": 7,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Stable row identity, e.g. 'guard-persistence'.",
                            },
                            "text": {
                                "type": "string",
                                "description": "Short concrete action, e.g. 'Add the replay guard'.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "active", "done"],
                                "description": "Current row state.",
                            },
                        },
                        "required": ["id", "text", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    },
}
