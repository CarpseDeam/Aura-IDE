"""Plan Review model-facing tool schema."""
from __future__ import annotations

from typing import Any

REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "review_implementation_plan",
        "description": (
            "Present a structured implementation plan for human review before "
            "making the first workspace mutation of this turn. Only offered "
            "when Plan Review is enabled. Call it after gathering enough "
            "evidence to state a real approach, and before any write, patch, "
            "delete, or other workspace edit. The result carries the "
            "human-approved plan — including any edits the user made — so "
            "continue with those final values, not your original draft. If "
            "the user cancels, the plan was not approved and mutations stay "
            "blocked for this turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "One or two sentences naming what this turn will accomplish.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: workspace-relative paths you expect to change. "
                        "Leave empty when files will be discovered as needed."
                    ),
                },
                "spec": {
                    "type": "string",
                    "description": (
                        "The concrete implementation approach: what changes, where, and how."
                    ),
                },
                "acceptance": {
                    "type": "string",
                    "description": "How you and the user will know the change is correct.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional: a short plain-language summary of the plan for display.",
                },
            },
            "required": ["goal", "spec", "acceptance"],
        },
    },
}

__all__ = ["REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF"]
