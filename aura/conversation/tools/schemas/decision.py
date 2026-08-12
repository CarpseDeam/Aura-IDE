"""Implementation decision record tool schema."""
from __future__ import annotations

from typing import Any

RECORD_IMPLEMENTATION_DECISION_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_implementation_decision",
        "description": (
            "Record the current working implementation decision once repository "
            "evidence supports a concrete choice. The record stands as the "
            "working decision going forward; ordinary additional information "
            "does not by itself invalidate it. Reopen or revise it when later "
            "evidence materially contradicts its basis, satisfies a "
            "reconsideration condition, an implementation or validation result "
            "disproves it, or the user changes the request — calling this tool "
            "again then records a newer decision that supersedes the earlier "
            "one. Performs no mutation, forces no next action, does not "
            "restrict which tools are available, and does not end discovery or "
            "change execution mode."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "description": (
                        "Concise statement of the concrete implementation "
                        "decision now supported by repository evidence."
                    ),
                },
                "targets": {
                    "type": "array",
                    "description": (
                        "Optional: workspace-relative paths currently expected "
                        "to participate in the implementation."
                    ),
                    "items": {"type": "string"},
                    "maxItems": 12,
                },
                "basis": {
                    "type": "array",
                    "description": (
                        "The repository/tool evidence that materially "
                        "supports this decision."
                    ),
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                },
                "reconsider_if": {
                    "type": "array",
                    "description": (
                        "Specific future evidence that would make reopening "
                        "this decision rational."
                    ),
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                },
                "validation": {
                    "type": "string",
                    "description": (
                        "The focused validation approach associated with "
                        "this decision."
                    ),
                },
            },
            "required": ["decision", "basis", "reconsider_if", "validation"],
            "additionalProperties": False,
        },
    },
}
