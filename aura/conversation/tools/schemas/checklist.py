"""Task Checklist tool schemas."""
from __future__ import annotations

from typing import Any

TASK_CHECKLIST_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_task_checklist",
        "description": (
            "Display-only progress checklist for the current request. Send the full ordered "
            "list every time, replacing the previous snapshot. Use it only when the work has "
            "several meaningful steps — skip it for a trivial one-step request. Keep one item "
            "'active' at a time; mark it 'done' and activate the next 'pending' item as real "
            "progress happens. Never splits the request into separate phases, assignments, or "
            "execution contexts, and never blocks or gates anything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Full ordered checklist snapshot. Reuse row ids across updates.",
                    "minItems": 1,
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
