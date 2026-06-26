"""Tool definition schema for research tools."""

from __future__ import annotations

from typing import Any

RESEARCH_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "research_current_info",
        "description": (
            "Fetch current external information from the live web and return grounded"
            " sources with titles, URLs, and extracted text. Use this whenever the user"
            " needs up-to-date info the model cannot know — recent events, current"
            " documentation, live API responses, or any external web data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to research — natural language query or specific question."
                    ),
                },
                "constraints": {
                    "type": "object",
                    "description": (
                        "Optional free-form constraints dict, e.g."
                        " {'max_pages': 3, 'timeout_seconds': 30}."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}
