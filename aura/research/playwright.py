"""PlaywrightResearcher — manages a local Playwright browser instance.

Uses ``playwright.sync_api`` directly. No MCP dependency.
"""

from __future__ import annotations

import os
import sys
import urllib.parse

from aura.research.extract import ParsedPage, normalize_text
from aura.research.limits import DEFAULT_LIMITS, ResearchLimits
from aura.research.models import Source
from aura.resources import get_resource_path


class PlaywrightResearcher:
    """Manages a local Playwright browser instance for web research.

    Create the instance, call ``start()``, then use ``search()`` and
    ``open()`` to fetch page content.  Call ``close()`` when done.
    All public methods short-circuit safely if ``start()`` never
    succeeded.
    """

    def __init__(
        self,
        limits: ResearchLimits = DEFAULT_LIMITS,
    ) -> None:
        self._limits = limits
        self._unavailable_reason = ""
        self._pw = None
        self._browser = None
        self._context = None

    # -- Lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Launch a local Playwright browser and create a browsing context.

        Returns True on success.  On failure sets ``_unavailable_reason``
        and returns False — never raises.
        """
        try:
            try:
                import playwright.sync_api  # noqa: F811
            except ImportError as exc:
                self._unavailable_reason = str(exc)
                return False

            # Determine if running packaged
            if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS") or "__compiled__" in globals():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(get_resource_path("ms-playwright"))

            self._pw = playwright.sync_api.sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context()
            self._context.set_default_navigation_timeout(20000)
            return True
        except Exception as exc:
            self._unavailable_reason = str(exc)
            # Tear down partial state — we are already in the error path
            self._context = None
            self._browser = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None
            return False

    def close(self) -> None:
        """Shut down the browser and clean up resources."""
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    # -- Public API ------------------------------------------------------

    def search(self, query: str) -> list[Source]:
        """Navigate to the search engine and return result links as Sources.

        Returns an empty list when the researcher is not started or on error.
        """
        if self._context is None:
            return []

        page = self._context.new_page()
        try:
            encoded = urllib.parse.quote(query)
            url = f"https://www.bing.com/search?q={encoded}"
            page.goto(url)

            links = page.eval_on_selector_all(
                "a",
                "els => els.map(e => ({href: e.href, text: e.innerText}))",
            )

            seen: set[str] = set()
            sources: list[Source] = []
            for link in links:
                href = link["href"]
                text = link["text"]
                if not href.startswith(("http://", "https://")):
                    continue
                if "bing.com" in href or "microsoft.com" in href:
                    continue
                if href in seen:
                    continue
                seen.add(href)
                sources.append(Source(url=href, title=text.strip()))

            return sources[: self._limits.max_pages]
        except Exception as exc:
            self._unavailable_reason = str(exc)
            return []
        finally:
            page.close()

    def open(self, url: str) -> ParsedPage:
        """Navigate to a URL and return the parsed page content.

        Returns an error-indicating ParsedPage when the researcher is
        not started or on error.
        """
        if self._context is None:
            return ParsedPage(clean_text="Researcher not started")

        page = self._context.new_page()
        try:
            page.goto(url)
            title = page.title()
            body_text = page.inner_text("body")
            return ParsedPage(
                url=url,
                title=title,
                clean_text=normalize_text(body_text),
            )
        except Exception as exc:
            # Return error text rather than raising to the caller
            return ParsedPage(clean_text=f"Error: {exc}")
        finally:
            page.close()
