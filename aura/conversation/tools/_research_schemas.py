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
                        "Optional free-form strategy dict guiding how research is performed."
                        " Supported fields: freshness (str), source_goal (str),"
                        " answer_shape (str), query_variants (list[str]),"
                        " allowed_domains (list[str]), blocked_domains (list[str]),"
                        " avoid (list[str]), max_searches (int), max_search_results (int),"
                        " max_pages_to_open (int), max_evidence_chars (int)."
                        " Also accepts legacy max_pages (int) and timeout_seconds (float)."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}
