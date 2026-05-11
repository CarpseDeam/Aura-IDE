"""Web tool handlers — search and fetch via external APIs."""
from __future__ import annotations

from typing import Any

from aura.conversation.tools.web import web_fetch, web_search


class WebHandler:
    """Web search and fetch tool handlers.

    Each handle_* method receives the raw args dict and returns a payload dict
    (the same shape as the underlying web.py functions return).
    """

    def handle_web_search(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search the web using Tavily."""
        query = args.get("query", "")
        max_results = int(args.get("max_results", 5))
        return web_search(query, max_results)

    def handle_web_fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Fetch and scrape the text content of a URL."""
        url = args.get("url", "")
        return web_fetch(url)
