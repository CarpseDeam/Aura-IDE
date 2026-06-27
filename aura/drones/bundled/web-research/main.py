"""Web Research Drone — skeleton phase.

Reads a goal payload from stdin via json-stdio protocol, extracts a research
query, and returns a structured receipt. No live browser research is performed
in this phase.
"""

import json
import sys


def _parse_query_from_goal(goal: str) -> str | None:
    """Extract the research query from the goal string.

    Supports:
    - A ``query:`` line (case-insensitive prefix).
    - Plain goal text when no ``query:`` line exists.
    Returns None if the goal is empty/whitespace-only.
    """
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


def build_success_receipt(query: str) -> dict:
    return {
        "ok": True,
        "summary": (
            f"Web Research Drone skeleton received query: {query}. "
            "Live browser research is not implemented yet."
        ),
        "query": query,
        "live_research_ready": False,
        "verified_facts": [],
        "sources": [],
        "evidence": [],
        "gaps": ["Live browser research is not implemented in this phase."],
        "confidence": "none",
        "trace": [
            {"step": "parse_goal", "status": "completed"},
            {"step": "live_browser_research", "status": "not_implemented"},
        ],
        "route_used": {
            "type": "local",
            "targets": [],
            "auth": "none",
            "reason": "Phase 2A skeleton only.",
            "fallback": "none",
        },
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

    result = build_success_receipt(query)
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
