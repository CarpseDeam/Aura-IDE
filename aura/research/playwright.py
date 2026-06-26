"""PlaywrightResearcher — manages a private Playwright MCP child process.

Uses ``aura.mcp_client.MCPClient`` directly.  No tools are registered
into any catalog or registry; the private MCP client is the sole
communication channel.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from aura.mcp_client import MCPClient

from aura.research.extract import ParsedPage, parse_call_result
from aura.research.limits import DEFAULT_LIMITS, ResearchLimits
from aura.research.models import Source


class PlaywrightResearcher:
    """Manages a private Playwright MCP subprocess for web research.

    Create the instance, call ``start()``, then use ``search()`` and
    ``open()`` to fetch page content.  Call ``close()`` when done.
    All public methods short-circuit safely if ``start()`` never
    succeeded.
    """

    def __init__(
        self,
        limits: ResearchLimits = DEFAULT_LIMITS,
        search_url_template: str = "https://www.bing.com/search?q={q}",
    ) -> None:
        self._limits = limits
        self._search_url_template = search_url_template

        cmd = "npx.cmd" if os.name == "nt" else "npx"
        self._server_command = [cmd, "@playwright/mcp@latest", "--headless", "--isolated"]

        self._client: MCPClient | None = None
        self._unavailable_reason: str = ""

    # -- Lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Launch the Playwright MCP server and establish a session.

        Returns True on success.  On failure sets ``_unavailable_reason``
        and returns False — never raises.
        """
        try:
            client = MCPClient(self._server_command)
            client.connect()
            self._client = client
            return True
        except Exception as exc:
            self._unavailable_reason = str(exc)
            self._client = None
            return False

    def close(self) -> None:
        """Shut down the MCP server and clean up resources."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- Internal helpers ------------------------------------------------

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> ParsedPage:
        """Call an MCP tool and parse the result into a ParsedPage.

        Returns an error-indicating ParsedPage when the client is not
        started.
        """
        if self._client is None:
            return ParsedPage(clean_text="Researcher not started")
        result: dict[str, Any] = self._client.call_tool(tool_name, arguments)
        return parse_call_result(result)

    # -- Public API ------------------------------------------------------

    def search(self, query: str) -> list[Source]:
        """Navigate to the search engine and return result links as Sources.

        Returns an empty list when the researcher is not started.
        """
        if self._client is None:
            return []

        encoded = urllib.parse.quote(query)
        url = self._search_url_template.format(q=encoded)

        self._call("browser_navigate", {"url": url})
        snapshot = self._call("browser_snapshot", {})

        sources: list[Source] = []
        for visible_text, href in snapshot.links:
            # Only real http(s) links
            if not href.startswith(("http://", "https://")):
                continue
            # Drop bing.com internal / promoted links
            if "bing.com" in href:
                continue
            sources.append(Source(url=href, title=visible_text))

        return sources[: self._limits.max_pages]

    def open(self, url: str) -> ParsedPage:
        """Navigate to a URL and return the parsed page content.

        Returns an error-indicating ParsedPage when the researcher is
        not started.
        """
        if self._client is None:
            return ParsedPage(clean_text="Researcher not started")

        self._call("browser_navigate", {"url": url})
        snapshot = self._call("browser_snapshot", {})
        return snapshot
