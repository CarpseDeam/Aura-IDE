"""PlaywrightResearcher — manages a local Playwright browser instance.

Uses ``playwright.sync_api`` directly. No MCP dependency.
"""

from __future__ import annotations

import urllib.parse

from aura.browser.runtime import BrowserRuntime
from aura.research.extract import ParsedPage, normalize_text
from aura.research.limits import DEFAULT_LIMITS, ResearchLimits
from aura.research.models import Source

_BING_CHROME_TITLES = {
    "all",
    "search",
    "images",
    "videos",
    "maps",
    "news",
    "shopping",
    "flights",
    "travel",
    "more",
    "tools",
}


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
        self._runtime = BrowserRuntime(headless=True)

    # -- Lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Launch a local Playwright browser and create a browsing context.

        Returns True on success.  On failure sets ``_unavailable_reason``
        and returns False — never raises.
        """
        if not self._runtime.start():
            self._unavailable_reason = self._runtime.unavailable_reason
            return False
        self._runtime.context.set_default_navigation_timeout(20000)
        return True

    def close(self) -> None:
        """Shut down the browser and clean up resources."""
        self._runtime.close()

    # -- Public API ------------------------------------------------------

    def search(self, query: str, max_results: int = 5) -> list[Source]:
        """Navigate to the search engine and return result links as Sources.

        Returns an empty list when the researcher is not started or on error.
        ``max_results`` caps the number of returned sources (default 5).
        """
        if self._runtime.context is None:
            return []

        page = self._runtime.context.new_page()
        try:
            encoded = urllib.parse.quote(query)
            url = f"https://www.bing.com/search?q={encoded}"
            page.goto(url)

            links = page.eval_on_selector_all(
                "li.b_algo h2 a",
                "els => els.map(e => ({href: e.href, text: "
                "(e.innerText || e.textContent || e.getAttribute('aria-label') || '')}))",
            )
            if not links:
                links = page.eval_on_selector_all(
                    "a",
                    "els => els.map(e => ({href: e.href, text: "
                    "(e.innerText || e.textContent || e.getAttribute('aria-label') || '')}))",
                )

            seen: set[str] = set()
            sources: list[Source] = []
            for link in links:
                href = (link.get("href") or "").strip()
                title = (link.get("text") or "").strip()
                if not href or href.startswith("javascript:"):
                    continue
                if not href.startswith(("http://", "https://")):
                    continue
                if not title:
                    continue
                parsed = urllib.parse.urlparse(href)
                hostname = parsed.hostname or ""
                path = parsed.path.rstrip("/")
                is_bing_url = hostname == "bing.com" or hostname.endswith(".bing.com")
                is_bing_redirect = is_bing_url and path.startswith("/ck/a")
                if is_bing_url and title.lower() in _BING_CHROME_TITLES:
                    continue
                if is_bing_url and not is_bing_redirect:
                    continue
                if href in seen:
                    continue
                seen.add(href)
                sources.append(Source(url=href, title=title))

            return sources[:max_results]
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
        if self._runtime.context is None:
            return ParsedPage(clean_text="Researcher not started")

        page = self._runtime.context.new_page()
        try:
            page.goto(url)
            final_url = page.url
            title = page.title()
            body_text = page.inner_text("body")
            return ParsedPage(
                url=final_url,
                title=title,
                clean_text=normalize_text(body_text),
            )
        except Exception as exc:
            # Return error text rather than raising to the caller
            return ParsedPage(clean_text=f"Error: {exc}")
        finally:
            page.close()
