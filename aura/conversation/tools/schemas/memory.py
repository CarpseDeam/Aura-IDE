"""Project-memory tool schemas."""
from __future__ import annotations

from typing import Any

PROJECT_MEMORY_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_project_memory",
            "description": (
                "Search the project's archival memory for past dispatch records "
                "and saved documentation. Use this when you need context from "
                "previous work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Default: 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_to_project_memory",
            "description": (
                "Save important information to the project's long-term memory "
                "for future retrieval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The content to save.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": (
                            "Optional structured metadata "
                            "(e.g., {'type': 'architecture_decision', 'tags': ['auth']})."
                        ),
                    },
                },
                "required": ["content"],
            },
        },
    },
]
