"""Task Checklist tool schemas."""
from __future__ import annotations

from typing import Any

TASK_CHECKLIST_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_task_checklist",
        "description": (
            "Display/progress bookkeeping for the overall user request: maintain a concise "
            "checklist as a full snapshot, replacing the previous one. Use it when work has "
            "multiple meaningful steps and update it as progress changes. Entries are progress "
            "markers within one continuous task, not phases, separate assignments, or context "
            "boundaries. This tool only displays progress; it never completes, blocks, or gates "
            "the task.\n\n"
            "Live progress cursor: while meaningful work remains, normally exactly one item is "
            "'active'. The initial snapshot marks the first meaningful item 'active' and the "
            "rest 'pending'. When the active item completes, update promptly: one replacement "
            "snapshot marks it 'done' and activates the next 'pending' item. Do not batch — "
            "never let several items finish and report them together later. Multiple items may "
            "become 'done' together only when one genuinely atomic action completed them "
            "together. Update at meaningful work boundaries, not on a timer. Whenever "
            "practical, pair a checklist update with the next useful tool call in the same "
            "response instead of an empty bookkeeping round. At completion, send a final "
            "snapshot marking the last active item 'done'."
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
