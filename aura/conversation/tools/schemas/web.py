"""Web search tool schema."""
from __future__ import annotations

from typing import Any

WEB_SEARCH_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Run live web research for latest/current facts, external docs/API examples, "
            "pricing, versions/releases/changelogs, schedules, current people/roles, "
            "error lookup, URLs, and external references. Returns sourced evidence with "
            "citations. Do not use web research for local repo, file, workspace, git, or "
            "ordinary coding questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The research question to search the web for.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional relevant context (local findings, user constraints) to include in the search request.",
                },
            },
            "required": ["question"],
        },
    },
}
