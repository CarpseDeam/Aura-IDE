"""Browser-backed search discovery for the Web Research Drone.

Uses the ``ResearchBrowserController`` as the sole browser owner.
All visibility, headless, profile, and runtime-route decisions live in the
controller — this module only asks for a browser and uses it.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models import FetchedSource, SourceTarget

try:
    from aura.browser.research_controller import ResearchBrowserController
    CONTROLLER_SUPPORTED = True
except ImportError:
    ResearchBrowserController = None  # type: ignore[assignment]
    CONTROLLER_SUPPORTED = False


SEARCH_BLOCKED_GAP = "Browser search was blocked by a CAPTCHA or verification page."
PAGE_BLOCKED_ERROR = "Page was blocked by a CAPTCHA or verification page."


@dataclass
class BrowserSearchResult:
    targets: list[SourceTarget] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    attempted: bool = False
    blocked: bool = False
    route_metadata: dict[str, Any] = field(default_factory=dict)


def _search_url(search_query: str) -> str:
    encoded = urllib.parse.quote_plus(search_query)
    return f"https://www.bing.com/search?q={encoded}"


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _search_host(host: str) -> bool:
    host = host.lower().split(":", 1)[0]
    search_hosts = (
        "bing.com",
        "www.bing.com",
        "google.com",
        "www.google.com",
        "duckduckgo.com",
        "html.duckduckgo.com",
        "search.yahoo.com",
        "search.brave.com",
    )
    return host in search_hosts or any(host.endswith(f".{item}") for item in search_hosts)


def _decode_bing_u(value: str) -> str:
    if not value:
        return ""
    candidate = value
    if candidate.startswith("a1"):
        candidate = candidate[2:]
    try:
        padded = candidate + "=" * (-len(candidate) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return decoded if decoded.startswith(("http://", "https://")) else ""


def _redirect_target_from_search_url(parsed: urllib.parse.ParseResult) -> str:
    values = urllib.parse.parse_qs(parsed.query)
    for key in ("uddg", "q", "url"):
        for value in values.get(key, []):
            decoded = urllib.parse.unquote(value)
            if decoded.startswith(("http://", "https://")):
                return decoded
    for value in values.get("u", []):
        decoded = _decode_bing_u(value)
        if decoded:
            return decoded
    return ""


def normalize_search_result_url(raw_href: str, base_url: str) -> str:
    """Normalize and filter a candidate URL from a browser search page."""
    href = html_lib.unescape(raw_href or "").strip()
    if not href:
        return ""
    href = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return ""

    if _search_host(parsed.netloc):
        redirected = _redirect_target_from_search_url(parsed)
        if redirected:
            return normalize_search_result_url(redirected, base_url)
        return ""

    clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
    return clean.rstrip("/")


def is_captcha_or_verification_page(title: str, url: str, body_text: str) -> bool:
    """Detect obvious CAPTCHA or bot-verification pages."""
    url_lower = (url or "").lower()
    if any(token in url_lower for token in ("captcha", "/sorry/", "challenge", "verification")):
        return True

    combined = " ".join([title or "", body_text or ""]).lower()
    patterns = (
        "captcha",
        "verify you are human",
        "human verification",
        "are you a robot",
        "unusual traffic",
        "automated queries",
        "complete the security check",
        "complete the challenge",
        "checking your browser",
        "security check to access",
        "detected unusual",
    )
    return any(pattern in combined for pattern in patterns)


def _visible_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        try:
            return page.inner_text("body", timeout=5000)
        except Exception:
            return ""


def extract_visible_result_links(page, base_url: str, max_targets: int = 8) -> list[SourceTarget]:
    """Extract visible result links from the current browser DOM."""
    js_code = """
    (maxAnchors) => {
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        const results = [];
        for (const anchor of anchors) {
            if (results.length >= maxAnchors) break;
            const style = window.getComputedStyle(anchor);
            const rect = anchor.getBoundingClientRect();
            if (
                style.display === 'none' ||
                style.visibility === 'hidden' ||
                parseFloat(style.opacity || '1') === 0 ||
                rect.width === 0 ||
                rect.height === 0
            ) {
                continue;
            }
            const text = (anchor.innerText || anchor.textContent || anchor.getAttribute('aria-label') || '').trim();
            results.push({
                href: anchor.href || anchor.getAttribute('href') || '',
                text: text,
            });
        }
        return results;
    }
    """
    try:
        raw_links = page.evaluate(js_code, max_targets * 6)
    except Exception:
        return []

    links: list[SourceTarget] = []
    seen: set[str] = set()
    for raw_link in raw_links:
        if not isinstance(raw_link, dict):
            continue
        url = normalize_search_result_url(str(raw_link.get("href") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        label = _clean_label(str(raw_link.get("text") or ""))[:160] or url
        links.append(SourceTarget(url=url, title=label, kind="candidate"))
        if len(links) >= max_targets:
            break
    return links


class BrowserResearchSession:
    """One browser-backed research session for discovery and source reads.

    Uses ``ResearchBrowserController`` as the single browser owner.
    """

    def __init__(self) -> None:
        self._controller: ResearchBrowserController | None = None
        self._page = None
        self._started = False
        self._closed = False
        self._receipt: dict[str, Any] = {}
        self.gaps: list[str] = []

    @property
    def route_metadata(self) -> dict[str, Any]:
        metadata = dict(self._receipt)
        metadata["browser_session_started"] = self._started
        metadata["browser_session_closed"] = self._closed
        return metadata

    def __enter__(self) -> BrowserResearchSession:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _add_gap(self, gap: str) -> None:
        if gap not in self.gaps:
            self.gaps.append(gap)

    def start(self) -> bool:
        """Start the research browser via the controller.

        The controller handles browser detection, profile, port, launch,
        CDP readiness, and page acquisition.  We just ask it to navigate
        to a blank page to prove the browser is alive.
        """
        if self._started and self._page is not None:
            return True
        if not CONTROLLER_SUPPORTED or ResearchBrowserController is None:
            self._add_gap("Browser research unavailable: ResearchBrowserController could not be imported.")
            return False

        self._controller = ResearchBrowserController()
        receipt = self._controller.start()
        self._receipt = receipt.to_dict()

        if not receipt.ok:
            phase_details = "; ".join(
                f"{phase}: {err}" for phase, err in receipt.phase_errors.items()
            ) or "Browser controller did not start."
            self._add_gap(f"Browser research unavailable: {phase_details}")
            self.close()
            return False

        self._page = self._controller.page
        if self._page is None:
            self._add_gap("Browser research unavailable: No page acquired.")
            self.close()
            return False

        self._started = True
        return True

    def discover(self, search_queries: list[str], max_targets: int = 8) -> BrowserSearchResult:
        result = BrowserSearchResult()
        if not search_queries:
            return result
        if not self.start():
            result.gaps.extend(self.gaps)
            result.route_metadata = self.route_metadata
            return result

        page = self._page
        for search_query in search_queries:
            if len(result.targets) >= max_targets:
                break
            url = _search_url(search_query)
            result.attempted = True
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=12000)
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
            except Exception as exc:
                result.gaps.append(f"Browser search navigation failed for '{search_query}': {exc}")
                continue

            title = page.title() or ""
            body_text = _visible_body_text(page)
            if is_captcha_or_verification_page(title, page.url, body_text):
                result.blocked = True
                result.gaps.append(SEARCH_BLOCKED_GAP)
                break

            for target in extract_visible_result_links(
                page,
                page.url or url,
                max_targets=max_targets - len(result.targets),
            ):
                if target.url not in {existing.url for existing in result.targets}:
                    result.targets.append(target)

        if result.attempted and not result.targets and not result.gaps:
            result.gaps.append("Browser search did not return candidate result links.")
        result.route_metadata = self.route_metadata
        return result

    def fetch_source(self, target: SourceTarget, fetched_at: str) -> FetchedSource:
        if not self.start():
            return FetchedSource(
                target=target,
                title=target.title,
                text="",
                fetched_at=fetched_at,
                ok=False,
                error="; ".join(self.gaps) or "Browser research session did not start.",
                route="browser",
            )

        page = self._page
        try:
            page.goto(target.url, wait_until="domcontentloaded", timeout=12000)
        except Exception as exc:
            return FetchedSource(
                target=target,
                title=target.title,
                text="",
                fetched_at=fetched_at,
                ok=False,
                error=f"Browser navigation failed: {exc}",
                route="browser",
            )

        title = page.title() or target.title
        text = _visible_body_text(page)
        final_url = page.url or target.url
        if is_captcha_or_verification_page(title, final_url, text):
            return FetchedSource(
                target=target,
                title=title,
                text="",
                fetched_at=fetched_at,
                ok=False,
                error=SEARCH_BLOCKED_GAP if target.kind == "search" else PAGE_BLOCKED_ERROR,
                route="browser",
                final_url=final_url,
            )

        normalized_text = re.sub(r"\s+", " ", text).strip()
        links = extract_visible_result_links(page, final_url, max_targets=10)
        return FetchedSource(
            target=target,
            title=title,
            text=text,
            fetched_at=fetched_at,
            ok=bool(normalized_text),
            error="" if normalized_text else "Browser fetch returned no readable body text.",
            excerpt=normalized_text[:1200],
            route="browser",
            links=links,
            final_url=final_url,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._controller is not None:
            try:
                self._controller.close()
            except Exception:
                pass


def discover_with_browser(search_queries: list[str], max_targets: int = 8) -> BrowserSearchResult:
    """Compatibility helper for direct browser discovery callers."""
    with BrowserResearchSession() as session:
        return session.discover(search_queries, max_targets=max_targets)
