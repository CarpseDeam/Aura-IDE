"""Aura Browser Service — extended browser API wrapping ResearchBrowserController.

``AuraBrowserService`` owns the service-level API surface: page/tab management,
navigation variants, observation, and future browser actions.  It delegates
browser lifecycle to ``ResearchBrowserController``.

This is the public API for Aura's browser operating layer.  Callers that only
need basic start/navigate/close can still use ``ResearchBrowserController``
directly.
"""

from __future__ import annotations

from typing import Any

from aura.browser.receipts import BrowserReceipt, BrowserSession, PageInfo
from aura.browser.research_controller import (
    CdpConnectError,
    ResearchBrowserController,
    _NAVIGATION_TIMEOUT,
)


class AuraBrowserService:
    """Public browser service for Aura — page management, navigation,
    observation, and future action support.

    Wraps ``ResearchBrowserController`` for lifecycle and extends it with
    a wider, receipt-driven API.  Every method returns a ``BrowserReceipt``.
    """

    def __init__(self, profile_subdir: str = "research",
                 search_url_pattern: str | None = None) -> None:
        self._ctrl = ResearchBrowserController(
            profile_subdir=profile_subdir,
            search_url_pattern=search_url_pattern,
        )

    # -- delegated properties -----------------------------------------------

    @property
    def page(self) -> Any:
        return self._ctrl.page

    @property
    def cdp_url(self) -> str | None:
        return self._ctrl.cdp_url

    @property
    def browser_pid(self) -> int | None:
        return self._ctrl.browser_pid

    @property
    def started(self) -> bool:
        return self._ctrl.started

    @property
    def session(self) -> BrowserSession:
        return self._ctrl.session

    @property
    def search_url_pattern(self) -> str:
        return self._ctrl.search_url_pattern

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> BrowserReceipt:
        return self._ctrl.start()

    def close(self) -> None:
        self._ctrl.close()

    def __enter__(self) -> AuraBrowserService:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- page / tab management ---------------------------------------------

    def list_pages(self) -> list[PageInfo]:
        """Return info for every open page/tab."""
        if self._ctrl._browser is None:
            return []
        result: list[PageInfo] = []
        try:
            for ctx in self._ctrl._browser.contexts:
                for page in ctx.pages:
                    idx = len(result)
                    try:
                        url, title = page.url or "", page.title() or ""
                    except Exception:
                        url, title = "", ""
                    result.append(PageInfo(
                        index=idx, url=url, title=title,
                        is_active=(page is self._ctrl._page),
                    ))
        except Exception:
            pass
        return result

    def acquire_active_page(self) -> BrowserReceipt:
        """Re-acquire the active page (anti-blank-slab policy)."""
        r = self._make_receipt("acquire_active_page", "acquire")
        if not self.started:
            return self._fail(r, "acquire", "Browser not started.")
        try:
            self._ctrl._acquire_page()
            return self._ok(r)
        except CdpConnectError as exc:
            return self._fail(r, "acquire", str(exc))

    def create_tab(self, url: str | None = None) -> BrowserReceipt:
        """Create a new tab, optionally navigating to *url*."""
        r = self._make_receipt("create_tab", "create")
        if not self.started:
            return self._fail(r, "create", "Browser not started.")
        try:
            contexts = self._ctrl._browser.contexts
            if contexts:
                page = contexts[0].new_page()
            else:
                ctx = self._ctrl._browser.new_context(no_viewport=True,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                page = ctx.new_page()
            self._ctrl._page = page
            if url:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
                except Exception as exc:
                    r.phase_errors["navigate"] = str(exc)
            r.action_status = "success"
            r.browser_ready = True
            r.requested_target = url or ""
            return self._ok(r)
        except Exception as exc:
            return self._fail(r, "create", str(exc))

    def switch_tab(self, index: int) -> BrowserReceipt:
        """Switch to the tab at 0-based *index*."""
        r = self._make_receipt("switch_tab", "switch")
        if not self.started:
            return self._fail(r, "switch", "Browser not started.")
        pages = self.list_pages()
        if index < 0 or index >= len(pages):
            return self._fail(r, "switch",
                              f"Tab index {index} out of range (0-{len(pages) - 1}).")
        try:
            target = None
            for ctx in self._ctrl._browser.contexts:
                ctx_pages = list(ctx.pages)
                for pi in pages:
                    if pi.index == index and pi.index < len(ctx_pages):
                        target = ctx_pages[pi.index]
                        break
                if target is not None:
                    break
            if target is not None:
                target.bring_to_front()
                self._ctrl._page = target
            r.action_status = "success"
            r.browser_ready = True
            return self._ok(r)
        except Exception as exc:
            return self._fail(r, "switch", str(exc))

    def close_current_tab(self) -> BrowserReceipt:
        """Close the current tab.  Keeps the session alive with a new blank
        page if the last tab was closed."""
        r = self._make_receipt("close_current_tab", "close")
        if not self.started:
            return self._fail(r, "close", "Browser not started.")
        pages_before = self.list_pages()
        if not pages_before:
            return self._fail(r, "close", "No tab to close.")
        try:
            old = self._ctrl._page
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            remaining: list[Any] = []
            for ctx in self._ctrl._browser.contexts:
                remaining.extend(list(ctx.pages))
            if remaining:
                self._ctrl._page = remaining[-1]
            elif self._ctrl._browser.contexts:
                self._ctrl._page = self._ctrl._browser.contexts[0].new_page()
            else:
                ctx = self._ctrl._browser.new_context(no_viewport=True,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                self._ctrl._page = ctx.new_page()
            r.action_status = "success"
            r.browser_ready = True
            return self._ok(r)
        except Exception as exc:
            return self._fail(r, "close", str(exc))

    # -- navigation API ----------------------------------------------------

    def navigate(self, url_or_query: str) -> BrowserReceipt:
        """Navigate to a URL or search query (backward-compatible)."""
        return self._ctrl.navigate(url_or_query)

    def goto_url(self, url: str) -> BrowserReceipt:
        """Navigate to a direct URL."""
        r = self._make_receipt("goto_url", "start")
        r.requested_target = url
        r.requested_url = url
        start_r = self.start()
        if not start_r.ok:
            r.phase_errors.update(start_r.phase_errors)
            return r
        r.browser_ready = True
        r.reused_existing = start_r.reused_existing
        page = self._ctrl._page
        if page is None:
            return self._fail(r, "navigate", "No active page.")
        r.first_navigated_url = url
        r.phase = "navigate"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
            r.final_active_url = page.url or url
            r.page_title = page.title() or ""
            r.navigation_status = "success"
        except Exception as exc:
            return self._fail(r, "navigate", str(exc))
        return self._ok(r)

    def search(self, query: str) -> BrowserReceipt:
        """Run a search query through the configured search engine."""
        import urllib.parse
        search_url = self.search_url_pattern.replace(
            "{query}", urllib.parse.quote_plus(query))
        r = self.goto_url(search_url)
        r.operation = "search"
        r.requested_target = query
        r.metadata["search_url"] = search_url
        r.metadata["search_query"] = query
        return r

    def reload(self) -> BrowserReceipt:
        """Reload the current page."""
        r = self._make_receipt("reload", "navigate")
        if not self.started or self._ctrl._page is None:
            return self._fail(r, "reload", "No active page.")
        r.browser_ready = True
        try:
            self._ctrl._page.reload(wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT)
            r.final_active_url = self._ctrl._page.url or ""
            r.page_title = self._ctrl._page.title() or ""
            r.navigation_status = "success"
        except Exception as exc:
            return self._fail(r, "reload", str(exc))
        return self._ok(r)

    # -- observation API ---------------------------------------------------

    def observe(self) -> BrowserReceipt:
        """Return browser-fact observation: URL, title, visible text, links.

        Each extraction phase runs independently so a partial failure
        (e.g. visible-text times out while URL succeeds) produces
        ``observation_status="partial"`` with per-phase errors.
        """
        r = self._make_receipt("observe", "observe")
        page = self._ctrl._page
        if not self.started or page is None:
            return self._fail(r, "observe", "No active page.")
        r.browser_ready = True

        # -- URL ---------------------------------------------------------
        try:
            r.final_active_url = page.url or ""
        except Exception as exc:
            r.phase_errors["observe_url"] = str(exc)

        # -- Title -------------------------------------------------------
        try:
            r.page_title = page.title() or ""
        except Exception as exc:
            r.phase_errors["observe_title"] = str(exc)

        # -- Visible text ------------------------------------------------
        try:
            text = self._visible_text(page)
            r.metadata["visible_text"] = text
            r.metadata["visible_text_length"] = len(text)
        except Exception as exc:
            r.phase_errors["observe_text"] = str(exc)
            r.metadata.setdefault("visible_text", "")
            r.metadata.setdefault("visible_text_length", 0)

        # -- Visible links -----------------------------------------------
        try:
            links = self._visible_links(page)
            r.metadata["links"] = [{"text": l["text"], "href": l["href"]} for l in links]
            r.metadata["link_count"] = len(links)
        except Exception as exc:
            r.phase_errors["observe_links"] = str(exc)
            r.metadata.setdefault("links", [])
            r.metadata.setdefault("link_count", 0)

        # -- Status ------------------------------------------------------
        if r.phase_errors:
            r.observation_status = "partial"
        else:
            r.observation_status = "success"
        return self._ok(r)

    def extract_visible_text(self) -> BrowserReceipt:
        """Extract visible body text. Returns in ``metadata["visible_text"]``."""
        r = self._make_receipt("extract_visible_text", "extract")
        page = self._ctrl._page
        if not self.started or page is None:
            return self._fail(r, "extract", "No active page.")
        r.browser_ready = True
        try:
            r.metadata["visible_text"] = self._visible_text(page)
            r.metadata["visible_text_length"] = len(r.metadata["visible_text"])
            r.final_active_url = page.url or ""
            r.page_title = page.title() or ""
            r.observation_status = "success"
        except Exception as exc:
            return self._fail(r, "extract", str(exc))
        return self._ok(r)

    def extract_links(self) -> BrowserReceipt:
        """Extract visible links. Returns in ``metadata["links"]``."""
        r = self._make_receipt("extract_links", "extract")
        page = self._ctrl._page
        if not self.started or page is None:
            return self._fail(r, "extract", "No active page.")
        r.browser_ready = True
        try:
            links = self._visible_links(page)
            r.metadata["links"] = [{"text": l["text"], "href": l["href"]} for l in links]
            r.metadata["link_count"] = len(links)
            r.final_active_url = page.url or ""
            r.page_title = page.title() or ""
            r.observation_status = "success"
        except Exception as exc:
            return self._fail(r, "extract", str(exc))
        return self._ok(r)

    def screenshot(self, path: str | None = None) -> BrowserReceipt:
        """Take a screenshot. Saves to *path* or stores base64 in metadata."""
        r = self._make_receipt("screenshot", "capture")
        page = self._ctrl._page
        if not self.started or page is None:
            return self._fail(r, "screenshot", "No active page.")
        r.browser_ready = True
        try:
            if path:
                page.screenshot(path=path)
                r.metadata["screenshot_path"] = path
            else:
                import base64
                r.metadata["screenshot_base64"] = (
                    base64.b64encode(page.screenshot()).decode("ascii"))
            r.observation_status = "success"
        except Exception as exc:
            return self._fail(r, "screenshot", str(exc))
        return self._ok(r)

    # -- action API skeleton -----------------------------------------------

    def click_selector(self, selector: str) -> BrowserReceipt:
        return self._not_implemented("click_selector", {"selector": selector})

    def click_text(self, text: str) -> BrowserReceipt:
        return self._not_implemented("click_text", {"search_text": text})

    def type_text(self, text: str, selector: str | None = None) -> BrowserReceipt:
        meta = {"text": text}
        if selector:
            meta["selector"] = selector
        return self._not_implemented("type_text", meta)

    def press_key(self, key: str) -> BrowserReceipt:
        return self._not_implemented("press_key", {"key": key})

    def scroll(self, delta_y: int | None = None, direction: str | None = None) -> BrowserReceipt:
        meta: dict[str, Any] = {}
        if delta_y is not None:
            meta["delta_y"] = delta_y
        if direction:
            meta["direction"] = direction
        return self._not_implemented("scroll", meta)

    def select_option(self, selector: str, value: str) -> BrowserReceipt:
        return self._not_implemented("select_option", {"selector": selector, "value": value})

    # -- internal helpers ---------------------------------------------------

    def _make_receipt(self, operation: str, phase: str) -> BrowserReceipt:
        s = self.session
        return BrowserReceipt(
            operation=operation, phase=phase,
            browser_executable=s.browser_executable,
            browser_profile_dir=s.browser_profile_dir,
            browser_pid=s.browser_pid, cdp_url=s.cdp_url,
            browser_ready=self.started,
            session_id=s.session_id, started_at=s.started_at,
            reused_existing=s.reused_existing,
            page_count=s.page_count,
            final_active_url=s.active_page_url,
            page_title=s.active_page_title,
        )

    _OBSERVATION_OPS = frozenset({
        "observe", "extract_visible_text", "extract_links", "screenshot",
        "acquire_active_page",
    })
    _ACTION_OPS = frozenset({
        "create_tab", "switch_tab", "close_current_tab",
        "click_selector", "click_text", "type_text", "press_key",
        "scroll", "select_option",
    })

    def _ok(self, r: BrowserReceipt) -> BrowserReceipt:
        """Sync session facts into the receipt after a successful operation.

        Does *not* overwrite a non-default status — if the caller already
        set ``observation_status`` to ``"partial"`` or ``action_status`` to
        a specific value, that value is preserved.
        """
        s = self.session
        r.page_index = s.active_page_index
        r.page_count = s.page_count
        if not r.final_active_url:
            r.final_active_url = s.active_page_url
        if not r.page_title:
            r.page_title = s.active_page_title
        if r.operation in self._OBSERVATION_OPS and r.observation_status == "not_started":
            r.observation_status = "success"
        if r.operation in self._ACTION_OPS and r.action_status == "not_started":
            r.action_status = "success"
        return r

    def _fail(self, r: BrowserReceipt, phase: str, msg: str) -> BrowserReceipt:
        r.phase_errors[phase] = msg
        if r.operation in self._OBSERVATION_OPS:
            r.observation_status = "failed"
        elif r.operation in self._ACTION_OPS:
            r.action_status = "failed"
        else:
            r.navigation_status = "failed"
        return r

    def _not_implemented(self, operation: str, meta: dict[str, Any]) -> BrowserReceipt:
        r = self._make_receipt(operation, "not_implemented")
        r.action_status = "not_implemented"
        r.metadata = meta
        r.phase_errors[operation] = "Action not yet implemented."
        return r

    @staticmethod
    def _visible_text(page: Any) -> str:
        """Extract visible body text. Robust against missing page / JS errors."""
        try:
            return page.locator("body").inner_text(timeout=5000)
        except Exception:
            try:
                return page.inner_text("body", timeout=5000)
            except Exception:
                return ""

    @staticmethod
    def _visible_links(page: Any) -> list[dict[str, str]]:
        """Extract visible links as ``[{"text": ..., "href": ...}]``."""
        js = """
        () => {
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            const results = [];
            for (const a of anchors) {
                const s = window.getComputedStyle(a);
                const r = a.getBoundingClientRect();
                if (s.display === 'none' || s.visibility === 'hidden' ||
                    parseFloat(s.opacity || '1') === 0 ||
                    r.width === 0 || r.height === 0) continue;
                const t = (a.innerText || a.textContent ||
                    a.getAttribute('aria-label') || '').trim();
                results.push({href: a.href || a.getAttribute('href') || '', text: t});
                if (results.length >= 50) break;
            }
            return results;
        }
        """
        try:
            raw = page.evaluate(js)
        except Exception:
            return []
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            href = str(item.get("href") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            links.append({"text": str(item.get("text") or "").strip(), "href": href})
        return links
