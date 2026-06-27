"""Web Research Drone - live sourced current-info research."""

import datetime as dt
from dataclasses import dataclass
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any

try:
    from aura.browser.runtime import BrowserRuntime

    BROWSER_SUPPORTED = True
except ImportError:
    BROWSER_SUPPORTED = False


TIMEZONES = "GMT|UTC|ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT"
TEAM_ALIASES = {
    "ENG": "England",
    "USA": "USA",
}


@dataclass(frozen=True)
class SourceTarget:
    url: str
    title: str = ""
    kind: str = "web"


@dataclass
class FetchedSource:
    target: SourceTarget
    title: str
    text: str
    fetched_at: str
    ok: bool
    error: str = ""
    excerpt: str = ""
    route: str = "http"


@dataclass
class ExtractedAnswer:
    answer: str
    verified_facts: list[str]
    evidence: list[dict[str, Any]]
    gaps: list[str]
    confidence: str


def _parse_query_from_goal(goal: str) -> str | None:
    """Extract the research query from the goal string."""
    if not goal or not goal.strip():
        return None

    lines = goal.split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("query:"):
            extracted = stripped[len("query:") :].strip()
            return extracted if extracted else None

    return goal.strip()


def classify_query(query: str) -> list[str]:
    tags: list[str] = []
    lower = query.lower()
    if "today" in lower or "tonight" in lower:
        tags.append("today")
    if "tomorrow" in lower:
        tags.append("tomorrow")
    if "world cup" in lower:
        tags.append("world_cup")
    if any(word in lower for word in ("schedule", "time", "play", "fixtures", "matches", "match", "game")):
        tags.append("schedule")
    if any(word in lower for word in ("latest", "current", "today", "tonight", "tomorrow", "recent", "now")):
        tags.append("current_info")
    return tags


def build_search_queries(query: str, tags: list[str]) -> list[str]:
    queries: list[str] = []
    if "world_cup" in tags and "schedule" in tags:
        queries.append("World Cup matches today schedule")
    queries.append(query.strip())

    seen: set[str] = set()
    unique: list[str] = []
    for item in queries:
        normalized = " ".join(item.split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            unique.append(normalized)
    return unique[:3]


def discover_sources(query: str, tags: list[str]) -> list[SourceTarget]:
    targets: list[SourceTarget] = []
    if "world_cup" in tags and "schedule" in tags:
        targets.append(
            SourceTarget(
                url="https://www.fifa.com/en/match-center",
                title="FIFA Match Centre",
                kind="official_schedule",
            )
        )
        targets.append(
            SourceTarget(
                url="https://www.espn.com/soccer/schedule",
                title="ESPN Soccer Schedule",
                kind="reputable_schedule",
            )
        )

    for search_query in build_search_queries(query, tags):
        encoded = urllib.parse.quote(search_query)
        targets.append(
            SourceTarget(
                url=f"https://html.duckduckgo.com/html/?q={encoded}",
                title=f"Search results for {search_query}",
                kind="search",
            )
        )

    seen: set[str] = set()
    unique: list[SourceTarget] = []
    for target in targets:
        if target.url in seen:
            continue
        seen.add(target.url)
        unique.append(target)
    return unique[:5]


def _empty_route(attempted: list[SourceTarget]) -> dict[str, Any]:
    return {
        "type": "none",
        "routes": [],
        "targets": [target.url for target in attempted],
        "attempted_targets": [target.url for target in attempted],
    }


def _mock_fetch_source(target: SourceTarget, fetched_at: str) -> FetchedSource | None:
    if os.environ.get("_AURA_MOCK_WEB_RESEARCH") != "1":
        return None

    lower_url = target.url.lower()
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
    return re.sub(r"\s+", " ", text).strip()


def _fetch_source(target: SourceTarget, now: dt.datetime) -> FetchedSource:
    fetched_at = now.isoformat()
    mocked = _mock_fetch_source(target, fetched_at)
    if mocked is not None:
        return mocked

    browser_error = ""
    if BROWSER_SUPPORTED:
        runtime = None
        try:
            runtime = BrowserRuntime(headless=True)
            if runtime.start():
                page = runtime.context.pages[0] if runtime.context.pages else runtime.context.new_page()
                page.goto(target.url, wait_until="domcontentloaded", timeout=15000)
                title = page.title() or target.title
                text = page.locator("body").inner_text(timeout=5000)
                excerpt = re.sub(r"\s+", " ", text).strip()[:1200]
                if text.strip():
                    return FetchedSource(
                        target=target,
                        title=title,
                        text=text,
                        fetched_at=fetched_at,
                        ok=True,
                        excerpt=excerpt,
                        route="browser",
                    )
                browser_error = "Browser fetch returned no readable body text."
            else:
                browser_error = "Browser runtime did not start."
        except Exception as exc:
            browser_error = f"Browser fetch error: {exc}"
        finally:
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:
                    pass

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
        return FetchedSource(
            target=target,
            title=title or target.title,
            text=text,
            fetched_at=fetched_at,
            ok=bool(text),
            error="" if text else "Fetched page had no readable body text.",
            excerpt=text[:1200],
            route="browser_http_fallback" if browser_error else "http",
        )
    except Exception as exc:
        error = f"HTTP fetch error: {exc}"
        if browser_error:
            error = f"{browser_error}; {error}"
        return FetchedSource(
            target=target,
            title=target.title,
            text="",
            fetched_at=fetched_at,
            ok=False,
            error=error,
            route="http",
        )


def fetch_sources(targets: list[SourceTarget], now: dt.datetime | None = None) -> list[FetchedSource]:
    now = now or dt.datetime.now().astimezone()
    fetched: list[FetchedSource] = []
    for target in targets[:5]:
        fetched.append(_fetch_source(target, now))
    return fetched


def schedule_subject_from_query(query: str) -> str:
    """Return a concise schedule subject for answer wording."""
    normalized = " ".join(str(query or "").strip().lower().split())
    if "world cup" in normalized:
        return "World Cup matches"
    if "play next" in normalized or "next match" in normalized or "next game" in normalized:
        return "Next match"

    match = re.search(
        r"\b(?:does|do|will|is|are)\s+(?P<team>[A-Za-z0-9 .&'-]{1,40}?)\s+play\b",
        str(query or ""),
        flags=re.IGNORECASE,
    )
    if match:
        team = re.sub(r"\s+", " ", match.group("team")).strip(" ?")
        team = re.sub(r"^(?:the)\s+", "", team, flags=re.IGNORECASE).strip()
        if team:
            return f"{team} match"

    return "Matches"


def _normalize_team_label(label: str) -> str:
    parts = re.split(r"\s+(vs\.?|v\.?|versus)\s+", label, flags=re.IGNORECASE)
    if len(parts) >= 3:
        home = _normalize_team_name(parts[0])
        away = _normalize_team_name(parts[2])
        return f"{home} vs {away}"
    return re.sub(r"\s+", " ", label).strip()


def _normalize_team_name(name: str) -> str:
    clean = re.sub(r"\s+", " ", name).strip(" .:-")
    clean = re.sub(r"\s+\bat\b$", "", clean, flags=re.IGNORECASE).strip()
    return TEAM_ALIASES.get(clean.upper(), clean)


def _normalize_time(value: str) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    suffix_match = re.search(rf"\b(?:{TIMEZONES})\b$", clean, flags=re.IGNORECASE)
    suffix = suffix_match.group(0).upper() if suffix_match else ""
    if suffix:
        clean = clean[: suffix_match.start()].strip()
    ampm_match = re.search(r"\b(?:AM|PM)\b$", clean, flags=re.IGNORECASE)
    ampm = ampm_match.group(0).upper() if ampm_match else ""
    if ampm:
        clean = clean[: ampm_match.start()].strip()
    result = clean
    if ampm:
        result = f"{result} {ampm}"
    if suffix:
        result = f"{result} {suffix}"
    return result


def _schedule_matches_from_text(text: str) -> list[re.Match[str]]:
    time_pattern = (
        r"(?P<time>(?:[01]?\d|2[0-3])(?::[0-5]\d)\s*(?:AM|PM|am|pm)?"
        rf"(?:\s*(?:{TIMEZONES}))?|(?:[1-9]|1[0-2])\s*(?:AM|PM|am|pm)"
        rf"(?:\s*(?:{TIMEZONES}))?)"
    )
    team = r"[A-Z][A-Za-z0-9 .&'()-]{1,40}"
    pattern = re.compile(
        rf"(?P<label>{team}\s+(?:vs\.?|v\.?|versus)\s+{team})\s*(?:[-:|,]|\bat\b)?\s*{time_pattern}",
        flags=re.IGNORECASE,
    )
    return list(pattern.finditer(text))


def _excerpt_around(text: str, start: int, end: int, radius: int = 220) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()


def extract_schedule_answer(
    query: str,
    fetched_sources: list[FetchedSource] | str,
    now: dt.datetime,
) -> ExtractedAnswer | tuple[str, list[str], list[dict[str, Any]], list[str], str]:
    """Extract one or more schedule entries from fetched evidence.

    A string input is accepted for older direct unit tests and returns the
    historical tuple shape.
    """
    legacy_text_input = isinstance(fetched_sources, str)
    if legacy_text_input:
        fetched_list = [
            FetchedSource(
                target=SourceTarget("about:legacy", "Evidence"),
                title="Evidence",
                text=str(fetched_sources),
                fetched_at=now.isoformat(),
                ok=bool(str(fetched_sources).strip()),
                excerpt=str(fetched_sources)[:1200],
            )
        ]
    else:
        fetched_list = fetched_sources

    entries: list[tuple[str, str, str, str]] = []
    evidence: list[dict[str, Any]] = []
    useful_text_seen = False

    for source in fetched_list:
        if not source.ok or not source.text.strip():
            continue
        useful_text_seen = True
        for match in _schedule_matches_from_text(source.text):
            label = _normalize_team_label(match.group("label"))
            time_string = _normalize_time(match.group("time"))
            fact = f"{label} is listed at {time_string}."
            if any(existing_fact == fact for _label, _time, existing_fact, _url in entries):
                continue
            entries.append((label, time_string, fact, source.target.url))
            evidence.append(
                {
                    "source_url": source.target.url,
                    "excerpt": _excerpt_around(source.text, match.start(), match.end()),
                    "supports_fact": fact,
                }
            )

    if not entries:
        no_parse_evidence: list[dict[str, Any]] = []
        for source in fetched_list:
            if source.ok and source.excerpt:
                item: dict[str, Any] = {"excerpt": source.excerpt}
                if not legacy_text_input:
                    item["source_url"] = source.target.url
                no_parse_evidence.append(item)
        result = ExtractedAnswer(
            answer="",
            verified_facts=[],
            evidence=no_parse_evidence[:2],
            gaps=[
                "No extractable schedule match and time were found in the fetched evidence."
                if useful_text_seen
                else "No evidence text was available to parse."
            ],
            confidence="low" if useful_text_seen else "none",
        )
        if legacy_text_input:
            return result.answer, result.verified_facts, result.evidence, result.gaps, result.confidence
        return result

    date_label = "today" if "today" in query.lower() else "tomorrow" if "tomorrow" in query.lower() else "requested date"
    subject = schedule_subject_from_query(query)
    rendered = [f"{label} at {time_string}" for label, time_string, _fact, _url in entries]
    if subject == "Next match":
        answer = f"{subject}: {rendered[0]}."
    else:
        answer = f"{subject} {date_label}: {'; '.join(rendered)}."

    facts = [fact for _label, _time, fact, _url in entries]
    gaps: list[str] = []
    if any(re.search(rf"\b(?:{TIMEZONES})\b", time_string, flags=re.IGNORECASE) for _label, time_string, _fact, _url in entries):
        gaps.append("Timezone conversion was not performed; the source timezone was preserved.")
    else:
        gaps.append("The source evidence did not include a timezone, so no timezone conversion was performed.")

    sources_by_fact: dict[str, set[str]] = {}
    for _label, _time, fact, url in entries:
        sources_by_fact.setdefault(fact, set()).add(url)
    if len({fact for _label, _time, fact, _url in entries}) != len(entries):
        gaps.append("Duplicate schedule facts appeared in fetched evidence.")

    result = ExtractedAnswer(
        answer=answer,
        verified_facts=facts,
        evidence=evidence,
        gaps=gaps,
        confidence="medium",
    )
    if legacy_text_input:
        return result.answer, result.verified_facts, result.evidence, result.gaps, result.confidence
    return result


def extract_answer(
    query: str,
    tags: list[str],
    fetched_sources: list[FetchedSource],
    now: dt.datetime,
) -> ExtractedAnswer:
    ok_sources = [source for source in fetched_sources if source.ok and source.text.strip()]
    if "schedule" in tags:
        extracted = extract_schedule_answer(query, ok_sources, now)
        assert isinstance(extracted, ExtractedAnswer)
        return extracted

    if not ok_sources:
        return ExtractedAnswer(
            answer="",
            verified_facts=[],
            evidence=[],
            gaps=["No useful evidence was fetched."],
            confidence="none",
        )

    return ExtractedAnswer(
        answer="",
        verified_facts=[],
        evidence=[
            {
                "source_url": source.target.url,
                "excerpt": source.excerpt or source.text[:1200],
            }
            for source in ok_sources[:2]
        ],
        gaps=["Fetched evidence did not clearly support a concise answer, so no answer was claimed."],
        confidence="low",
    )


def _source_status(source: FetchedSource) -> dict[str, Any]:
    return {
        "title": source.title or source.target.title,
        "url": source.target.url,
        "fetched_at": source.fetched_at,
        "status": "ok" if source.ok else "failed",
        "ok": source.ok,
        "error": source.error,
        "excerpt": source.excerpt,
    }


def _build_route_used(targets: list[SourceTarget], fetched_sources: list[FetchedSource]) -> dict[str, Any]:
    routes = [source.route for source in fetched_sources if source.route]
    if not routes:
        return _empty_route(targets)
    route_type = "mixed" if len(set(routes)) > 1 else routes[0]
    return {
        "type": route_type,
        "routes": routes,
        "targets": [target.url for target in targets],
        "attempted_targets": [source.target.url for source in fetched_sources],
    }


def build_result(
    query: str,
    tags: list[str],
    targets: list[SourceTarget],
    fetched_sources: list[FetchedSource],
    extracted: ExtractedAnswer,
) -> dict[str, Any]:
    failed = [source for source in fetched_sources if not source.ok]
    successful = [source for source in fetched_sources if source.ok and source.text.strip()]
    gaps: list[str] = []
    for source in failed:
        gaps.append(f"Could not reach {source.target.url}: {source.error}")
    gaps.extend(extracted.gaps)
    if failed and successful:
        gaps.append("At least one source failed, but another source supplied extractable evidence.")
    if not successful:
        gaps.append("No useful evidence was fetched from attempted sources.")

    trace = [
        {"step": "parse_goal", "status": "completed"},
        {"step": "classify_query", "status": "completed", "tags": tags},
        {"step": "build_search_queries", "status": "completed", "queries": build_search_queries(query, tags)},
        {"step": "discover_sources", "status": "completed", "targets": [target.url for target in targets]},
        {
            "step": "fetch_sources",
            "status": "completed",
            "attempted": len(fetched_sources),
            "succeeded": len(successful),
            "failed": len(failed),
        },
        {"step": "extract_answer", "status": "completed", "confidence": extracted.confidence},
        {"step": "build_result", "status": "completed"},
    ]

    confidence = extracted.confidence
    if not extracted.answer and confidence not in {"none", "low"}:
        confidence = "low"
    if not successful:
        confidence = "none"

    return {
        "ok": True,
        "summary": "Completed live web research." if successful else "Web research completed without usable evidence.",
        "query": query,
        "answer": extracted.answer,
        "verified_facts": extracted.verified_facts,
        "sources": [_source_status(source) for source in fetched_sources],
        "evidence": extracted.evidence,
        "gaps": gaps,
        "confidence": confidence,
        "trace": trace,
        "route_used": _build_route_used(targets, fetched_sources),
    }


def build_failure_receipt(error: str, summary: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "summary": summary,
    }


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        payload: dict[str, Any] = {}
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            result = build_failure_receipt(
                f"Invalid JSON input: {exc}",
                "Web Research Drone could not run because the input was not valid JSON.",
            )
            print(json.dumps(result))
            return

    goal = payload.get("goal", "")
    query = _parse_query_from_goal(goal)
    if query is None:
        result = build_failure_receipt(
            "query is required",
            "Web Research Drone could not run because no query was provided.",
        )
        print(json.dumps(result))
        return

    now = dt.datetime.now().astimezone()
    tags = classify_query(query)
    targets = discover_sources(query, tags)
    fetched_sources = fetch_sources(targets, now)
    extracted = extract_answer(query, tags, fetched_sources, now)
    result = build_result(query, tags, targets, fetched_sources, extracted)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        result = build_failure_receipt(
            str(exc),
            f"Web Research Drone encountered an error: {exc}",
        )
        print(json.dumps(result))
