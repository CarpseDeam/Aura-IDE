"""Source discovery, fetching, page reading, and link extraction."""

from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from browser_search import (
    BrowserResearchSession,
    PAGE_BLOCKED_ERROR,
    SEARCH_BLOCKED_GAP,
    is_captcha_or_verification_page,
    normalize_search_result_url,
)
from models import FetchedSource, SourceTarget
from query import build_search_queries

@dataclass
class SourceDiscovery:
    targets: list[SourceTarget]
    gaps: list[str] = field(default_factory=list)
    route_metadata: dict[str, Any] = field(default_factory=dict)


def _user_provided_url_targets(query: str) -> list[SourceTarget]:
    targets: list[SourceTarget] = []
    for direct_url in re.findall(r"https?://[^\s)>\"]+", query):
        targets.append(SourceTarget(url=direct_url.rstrip(".,;"), title=direct_url.rstrip(".,;"), kind="candidate"))
    return targets


def _unique_targets(targets: list[SourceTarget], limit: int = 8) -> list[SourceTarget]:
    seen: set[str] = set()
    unique: list[SourceTarget] = []
    for target in targets:
        if target.url in seen:
            continue
        seen.add(target.url)
        unique.append(target)
        if len(unique) >= limit:
            break
    return unique


def discover_sources(query: str, tags: list[str]) -> list[SourceTarget]:
    return discover_sources_with_gaps(query, tags).targets


def discover_sources_with_gaps(
    query: str,
    tags: list[str],
    browser_session: BrowserResearchSession | None = None,
) -> SourceDiscovery:
    user_targets = _user_provided_url_targets(query)
    discovered_targets: list[SourceTarget] = []
    gaps: list[str] = []
    route_metadata: dict[str, Any] = {}

    if _mock_web_research_enabled():
        discovered_targets = _mock_browser_discovered_targets(query, tags)
    elif os.environ.get("_AURA_WEB_RESEARCH_DISABLE_BROWSER_DISCOVERY") == "1":
        gaps.append("Browser-backed source discovery was disabled for this run.")
    else:
        owns_session = browser_session is None
        session = browser_session or BrowserResearchSession()
        try:
            browser_result = session.discover(build_search_queries(query, tags), max_targets=6)
            discovered_targets.extend(browser_result.targets)
            gaps.extend(browser_result.gaps)
            route_metadata = dict(getattr(browser_result, "route_metadata", {}) or {})
        finally:
            if owns_session:
                session.close()

    targets = _unique_targets(discovered_targets + user_targets)
    return SourceDiscovery(targets=targets, gaps=gaps, route_metadata=route_metadata)


def _load_mock_fixture() -> dict[str, Any]:
    raw = os.environ.get("_AURA_WEB_RESEARCH_MOCK_FIXTURE", "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _search_query_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query_values = urllib.parse.parse_qs(parsed.query).get("q", [])
    return query_values[0] if query_values else ""


def _mock_web_research_enabled() -> bool:
    return os.environ.get("_AURA_MOCK_WEB_RESEARCH") == "1" or bool(_load_mock_fixture())


def _mock_search_target(search_query: str) -> SourceTarget:
    encoded = urllib.parse.quote(search_query)
    return SourceTarget(
        url=f"https://browser-search.local/search?q={encoded}",
        title=f"Browser search results for {search_query}",
        kind="search",
    )


def _mock_browser_discovered_targets(query: str, tags: list[str]) -> list[SourceTarget]:
    fixture = _load_mock_fixture()
    targets: list[SourceTarget] = []
    for search_query in build_search_queries(query, tags):
        search_target = _mock_search_target(search_query)
        results = (
            _mock_fixture_results_for_target(fixture, search_target)
            if fixture
            else _builtin_mock_results(search_target)
        )
        for result in results:
            url = str(result.get("url") or "")
            if not url:
                continue
            targets.append(
                SourceTarget(
                    url=url,
                    title=str(result.get("title") or url),
                    kind="candidate",
                )
            )
    return _unique_targets(targets)


def _mock_fixture_results_for_target(fixture: dict[str, Any], target: SourceTarget) -> list[dict[str, Any]]:
    raw_results = fixture.get("results", [])
    if isinstance(raw_results, dict):
        search_query = _search_query_from_url(target.url)
        raw_results = raw_results.get(search_query) or raw_results.get("default") or []
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, Any]] = []
    for item in raw_results:
        if isinstance(item, str):
            results.append({"url": item, "title": item})
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            results.append(
                {
                    "url": str(item["url"]),
                    "title": str(item.get("title") or item["url"]),
                }
            )
    return results


def _mock_fixture_page_for_url(fixture: dict[str, Any], url: str) -> dict[str, Any] | None:
    raw_pages = fixture.get("pages", {})
    if not isinstance(raw_pages, dict) or url not in raw_pages:
        return None
    raw_page = raw_pages[url]
    if isinstance(raw_page, str):
        return {"title": url, "text": raw_page, "ok": True}
    if isinstance(raw_page, dict):
        return raw_page
    return None


def _mock_search_page(target: SourceTarget, fetched_at: str, results: list[dict[str, Any]]) -> FetchedSource:
    links = [
        SourceTarget(url=str(result["url"]), title=str(result.get("title") or result["url"]), kind="candidate")
        for result in results
    ]
    text_parts = [f"Search result: {link.title} {link.url}" for link in links]
    text = " ".join(text_parts) or "No mocked search results."
    return FetchedSource(
        target=target,
        title=target.title or "Mock Search",
        text=text,
        fetched_at=fetched_at,
        ok=bool(results),
        error="" if results else "No mocked search results.",
        excerpt=text[:1200],
        route="mock",
        links=links,
    )


def _builtin_mock_results(target: SourceTarget) -> list[dict[str, Any]]:
    query = _search_query_from_url(target.url).lower()
    if "fail" in query or "not found" in query:
        return [{"url": "https://mock.local/fail", "title": "Mock Failure"}]
    if "world cup" in query:
        return [{"url": "https://mock.local/world-cup-schedule", "title": "World Cup Schedule"}]
    if "python" in query and ("version" in query or "latest" in query):
        return [{"url": "https://mock.local/python-latest", "title": "Python Downloads"}]
    if "nuitka" in query:
        return [{"url": "https://mock.local/nuitka-release", "title": "Nuitka Changelog"}]
    if "gpt-4.1" in query or "gpt 4.1" in query or "openai" in query and "price" in query:
        return [{"url": "https://mock.local/openai-pricing", "title": "OpenAI API Pricing"}]
    if "microsoft" in query and "ceo" in query:
        return [{"url": "https://mock.local/microsoft-leadership", "title": "Microsoft Leadership"}]
    return [{"url": "https://mock.local/no-clear-answer", "title": "Mock Search Result"}]


def _builtin_mock_page(url: str) -> dict[str, Any] | None:
    pages: dict[str, dict[str, Any]] = {
        "https://mock.local/fail": {
            "ok": False,
            "title": "Mock Failure",
            "text": "",
            "error": "HTTP fetch error: 404 Not Found",
        },
        "https://mock.local/world-cup-schedule": {
            "title": "World Cup Schedule",
            "text": "World Cup Matches Today: USA vs ENG 8:00 PM GMT",
        },
        "https://mock.local/python-latest": {
            "title": "Python Downloads",
            "text": "Latest Python release: Python 3.14.0 is the newest stable version.",
        },
        "https://mock.local/nuitka-release": {
            "title": "Nuitka Changelog",
            "text": (
                "Nuitka 2.7.12 is the latest release. "
                "Changes in Nuitka 2.7.12 include improved standalone packaging and fixes for Python 3.14."
            ),
        },
        "https://mock.local/openai-pricing": {
            "title": "OpenAI API Pricing",
            "text": "GPT-4.1 pricing is $2.00 per 1M input tokens and $8.00 per 1M output tokens.",
        },
        "https://mock.local/microsoft-leadership": {
            "title": "Microsoft Leadership",
            "text": "Satya Nadella is Chairman and Chief Executive Officer of Microsoft.",
        },
        "https://mock.local/no-clear-answer": {
            "title": "Mock Search Result",
            "text": "This page mentions general background information but does not provide the requested current answer.",
        },
    }
    return pages.get(url)


def _mock_fetch_source(target: SourceTarget, fetched_at: str) -> FetchedSource | None:
    fixture = _load_mock_fixture()
    fixture_page = _mock_fixture_page_for_url(fixture, target.url)
    if fixture_page is not None:
        ok = bool(fixture_page.get("ok", True))
        text = str(fixture_page.get("text", ""))
        error = str(fixture_page.get("error", ""))
        links = [
            SourceTarget(url=str(link.get("url")), title=str(link.get("title") or link.get("url")), kind="candidate")
            for link in fixture_page.get("links", [])
            if isinstance(link, dict) and isinstance(link.get("url"), str)
        ]
        return FetchedSource(
            target=target,
            title=str(fixture_page.get("title") or target.title),
            text=text,
            fetched_at=fetched_at,
            ok=ok,
            error="" if ok else error or "Mocked source failed.",
            excerpt=text[:1200],
            route="mock",
            links=links,
        )
    if target.kind in {"search", "search_fallback"} and fixture:
        return _mock_search_page(target, fetched_at, _mock_fixture_results_for_target(fixture, target))

    if os.environ.get("_AURA_MOCK_WEB_RESEARCH") != "1":
        return None

    lower_url = target.url.lower()
    builtin_page = _builtin_mock_page(target.url)
    if builtin_page is not None:
        ok = bool(builtin_page.get("ok", True))
        text = str(builtin_page.get("text", ""))
        return FetchedSource(
            target=target,
            title=str(builtin_page.get("title") or target.title),
            text=text,
            fetched_at=fetched_at,
            ok=ok,
            error="" if ok else str(builtin_page.get("error") or "Mocked source failed."),
            excerpt=text[:1200],
            route="mock",
        )
    if target.kind in {"search", "search_fallback"}:
        return _mock_search_page(target, fetched_at, _builtin_mock_results(target))
    if "fail" in lower_url or "not%20found" in lower_url:
        return FetchedSource(
            target=target,
            title=target.title or "Mock Failure",
            text="",
            fetched_at=fetched_at,
            ok=False,
            error="HTTP fetch error: 404 Not Found",
            route="http",
        )
    if "fifa.com" in lower_url:
        text = "World Cup Matches Today: USA vs ENG 8:00 PM GMT"
        return FetchedSource(
            target=target,
            title=target.title or "FIFA Mock",
            text=text,
            fetched_at=fetched_at,
            ok=True,
            excerpt=text,
            route="http",
        )
    if "world%20cup" in lower_url or "world cup" in lower_url:
        text = "Search result: World Cup Matches Today: USA vs ENG 8:00 PM GMT"
        return FetchedSource(
            target=target,
            title=target.title or "Mock Search",
            text=text,
            fetched_at=fetched_at,
            ok=True,
            excerpt=text,
            route="http",
        )

    text = "Mocked search result. Evidence exists, but no concise answer is extractable."
    return FetchedSource(
        target=target,
        title=target.title or "Mock Search",
        text=text,
        fetched_at=fetched_at,
        ok=True,
        excerpt=text,
        route="http",
    )


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _normalize_result_url(raw_href: str, base_url: str) -> str:
    return normalize_search_result_url(raw_href, base_url)


def _extract_links_from_html(html: str, base_url: str) -> list[SourceTarget]:
    links: list[SourceTarget] = []
    seen: set[str] = set()
    anchor_pattern = re.compile(
        r"(?is)<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<label>.*?)</a>"
    )
    for match in anchor_pattern.finditer(html):
        url = _normalize_result_url(match.group("href"), base_url)
        if not url or url in seen:
            continue
        label = _strip_html(match.group("label"))[:160] or url
        seen.add(url)
        links.append(SourceTarget(url=url, title=label, kind="candidate"))
        if len(links) >= 10:
            break
    return links


def _extract_links_from_text(text: str) -> list[SourceTarget]:
    links: list[SourceTarget] = []
    seen: set[str] = set()
    for raw in re.findall(r"https?://[^\s<>()\"']+", text):
        url = raw.rstrip(".,;]")
        if url in seen:
            continue
        seen.add(url)
        links.append(SourceTarget(url=url, title=url, kind="candidate"))
        if len(links) >= 10:
            break
    return links


def _fetch_source(target: SourceTarget, now: dt.datetime) -> FetchedSource:
    fetched_at = now.isoformat()
    mocked = _mock_fetch_source(target, fetched_at)
    if mocked is not None:
        return mocked

    req = urllib.request.Request(
        target.url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Aura/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = _strip_html(title_match.group(1)) if title_match else target.title
        text = _strip_html(html)
        if is_captcha_or_verification_page(title, target.url, text):
            return FetchedSource(
                target=target,
                title=title or target.title,
                text="",
                fetched_at=fetched_at,
                ok=False,
                error=SEARCH_BLOCKED_GAP if target.kind == "search" else PAGE_BLOCKED_ERROR,
                route="http",
            )
        links = _extract_links_from_html(html, target.url) if target.kind in {"search", "search_fallback"} else []
        return FetchedSource(
            target=target,
            title=title or target.title,
            text=text,
            fetched_at=fetched_at,
            ok=bool(text),
            error="" if text else "Fetched page had no readable body text.",
            excerpt=text[:1200],
            route="http",
            links=links,
        )
    except Exception as exc:
        error = f"HTTP fetch error: {exc}"
        return FetchedSource(
            target=target,
            title=target.title,
            text="",
            fetched_at=fetched_at,
            ok=False,
            error=error,
            route="http",
        )


def fetch_sources(
    targets: list[SourceTarget],
    now: dt.datetime | None = None,
    browser_session: BrowserResearchSession | None = None,
) -> list[FetchedSource]:
    now = now or dt.datetime.now().astimezone()
    fetched: list[FetchedSource] = []
    for target in targets[:8]:
        fetched_at = now.isoformat()
        mocked = _mock_fetch_source(target, fetched_at)
        if mocked is not None:
            fetched.append(mocked)
        elif browser_session is not None:
            fetched.append(browser_session.fetch_source(target, fetched_at))
        else:
            fetched.append(_fetch_source(target, now))
    return fetched


def discover_candidate_sources(
    fetched_sources: list[FetchedSource],
    existing_targets: list[SourceTarget],
) -> list[SourceTarget]:
    seen = {target.url for target in existing_targets}
    candidates: list[SourceTarget] = []
    for source in fetched_sources:
        links = list(source.links)
        if source.target.kind in {"search", "search_fallback"} and not links:
            links = _extract_links_from_text(source.text)
        for link in links:
            if link.url in seen:
                continue
            seen.add(link.url)
            candidates.append(link)
            if len(candidates) >= 8:
                return candidates
    return candidates
