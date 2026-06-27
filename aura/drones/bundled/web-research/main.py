"""Web Research Drone — live sourced current-info research."""

import datetime
import json
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


def _parse_query_from_goal(goal: str) -> str | None:
    """Extract the research query from the goal string."""
    if not goal or not goal.strip():
        return None

    lines = goal.split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("query:"):
            extracted = stripped[len("query:"):].strip()
            return extracted if extracted else None

    # No query: line found — use the whole goal as the query
    return goal.strip()


def classify_query(query: str) -> list[str]:
    tags = []
    lower = query.lower()
    if "today" in lower or "tonight" in lower: tags.append("today")
    if "tomorrow" in lower: tags.append("tomorrow")
    if "world cup" in lower: tags.append("world_cup")
    if "schedule" in lower or "time" in lower or "play" in lower or "fixtures" in lower: tags.append("schedule")
    return tags


def build_search_targets(query: str, tags: list[str]) -> list[str]:
    urls = []
    if "world_cup" in tags and "schedule" in tags:
        urls.append("https://www.fifa.com/en/match-center")
    encoded_query = urllib.parse.quote(query)
    urls.append(f"https://html.duckduckgo.com/html/?q={encoded_query}")
    return urls


def fetch_evidence(url: str) -> tuple[str, str, dict]:
    """Returns (title, text, route_metadata)."""
    route_used = {
        "type": "http",
        "targets": [url],
        "auth": "none",
        "reason": "lightweight HTTP fetch",
        "fallback": "none",
    }

    import os
    if os.environ.get("_AURA_MOCK_WEB_RESEARCH") == "1":
        if "fifa.com" in url or "world%20cup" in url.lower() or "world cup" in url.lower():
            return "FIFA Mock", "World Cup Matches Today: USA vs ENG 8:00 PM GMT", route_used
        if "fail" in url.lower() or "not%20found" in url.lower():
            return "Error Mock", "HTTP fetch error: 404 Not Found", route_used
        return "Mock Search", "Mocked duckduckgo search result.", route_used

    title = ""
    text = ""
    if BROWSER_SUPPORTED:
        runtime = BrowserRuntime(headless=True)
        if runtime.start():
            try:
                page = runtime.context.pages[0] if runtime.context.pages else runtime.context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                title = page.title()
                text = page.locator("body").inner_text()
                route_used.update(runtime.route_metadata)
            except Exception as e:
                text = f"Browser fetch error: {e}"
            finally:
                runtime.close()
            return title, text, route_used

    # Fallback to urllib if browser not supported or failed to start
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
            title = "Search Results"
            # Strip tags rudimentarily
            text = re.sub(r'<[^>]+>', ' ', html)
    except Exception as e:
        text = f"HTTP fetch error: {e}"

    return title, text, route_used


def extract_schedule_answer(
    query: str,
    text: str,
    now: datetime.datetime,
) -> tuple[str, list[str], list[dict], list[str], str]:
    """Extract a tiny schedule answer from evidence text.

    Returns answer, verified_facts, evidence, gaps, confidence.
    """
    snippet = (text[:2000] if len(text) > 2000 else text).strip()
    if not snippet:
        return "", [], [], ["No evidence text was available to parse."], "none"

    time_pattern = (
        r"\b(?P<time>(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:AM|PM|am|pm)?"
        r"(?:\s*(?:GMT|UTC|ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT))?)\b"
    )
    match = re.search(
        r"(?P<label>[A-Z][A-Za-z .'-]{1,40}\s+(?:vs\.?|v\.?|versus)\s+[A-Z][A-Za-z .'-]{1,40})\s+"
        + time_pattern,
        snippet,
    )
    if not match:
        return (
            "",
            [],
            [{"excerpt": snippet}],
            ["No extractable schedule match and time were found in the fetched evidence."],
            "low",
        )

    label = re.sub(r"\s+", " ", match.group("label")).strip()
    label = re.sub(r"\bENG\b", "England", label)
    time_string = re.sub(r"\s+", " ", match.group("time")).strip().upper()
    date_label = "today" if "today" in query.lower() else "tomorrow" if "tomorrow" in query.lower() else "requested date"
    answer = f"World Cup matches {date_label}: {label} at {time_string}."
    fact = f"{label} is listed at {time_string}."
    gaps = []
    if re.search(r"\b(?:GMT|UTC|ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT)\b", time_string):
        gaps.append("Timezone conversion was not performed; the source timezone was preserved.")
    else:
        gaps.append("The source evidence did not include a timezone, so no timezone conversion was performed.")

    _ = now
    return answer, [fact], [{"excerpt": snippet}], gaps, "medium"


def synthesize_answer(query: str, tags: list[str], url: str, title: str, text: str, route_used: dict) -> dict:
    trace = [
        {"step": "parse_goal", "status": "completed"},
        {"step": "classify_query", "status": "completed", "tags": tags},
        {"step": "build_search_targets", "status": "completed"},
        {"step": "fetch_evidence", "status": "completed", "url": url},
    ]

    now = datetime.datetime.now().astimezone()
    iso_time = now.isoformat()
    local_time = now.strftime("%Y-%m-%d %I:%M %p %Z")

    if not text or "fetch error:" in text.lower():
        return {
            "ok": True,
            "summary": "Failed to fetch evidence.",
            "query": query,
            "answer": "",
            "verified_facts": [],
            "sources": [],
            "evidence": [],
            "gaps": [f"Could not reach {url}: {text}"],
            "confidence": "none",
            "trace": trace + [{"step": "synthesize_answer", "status": "completed"}],
            "route_used": route_used,
        }

    snippet = text[:2000] if len(text) > 2000 else text
    answer = ""
    verified_facts: list[str] = []
    evidence = [{"source_url": url, "excerpt": snippet.strip()}]
    gaps: list[str] = []
    confidence = "low"

    if "schedule" in tags:
        (
            answer,
            verified_facts,
            extracted_evidence,
            gaps,
            confidence,
        ) = extract_schedule_answer(query, text, now)
        evidence = [
            {"source_url": url, **item}
            for item in extracted_evidence
        ]
    else:
        gaps = ["No extractable answer was parsed from the fetched evidence."]

    return {
        "ok": True,
        "summary": "Completed live web research.",
        "query": query,
        "answer": answer,
        "verified_facts": verified_facts,
        "sources": [
            {
                "url": url,
                "title": title,
                "fetched_at": iso_time
            }
        ],
        "evidence": evidence,
        "gaps": gaps,
        "confidence": confidence,
        "trace": trace + [{"step": "synthesize_answer", "status": "completed"}],
        "route_used": route_used,
    }


def build_failure_receipt(error: str, summary: str) -> dict:
    return {
        "ok": False,
        "error": error,
        "summary": summary,
    }


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        payload = {}
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

    tags = classify_query(query)
    targets = build_search_targets(query, tags)
    target_url = targets[0] if targets else "https://html.duckduckgo.com"

    title, text, route_used = fetch_evidence(target_url)

    result = synthesize_answer(query, tags, target_url, title, text, route_used)
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
