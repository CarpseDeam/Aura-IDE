"""Private parsing helpers for Worker result payloads."""

from __future__ import annotations

import json
from typing import Any


def _compact_title(text: str, limit: int = 90) -> str:
    title = " ".join(text.strip().split())
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "..."


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_string(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _failure_text(parsed: dict[str, Any], *, fallback: str) -> str:
    return str(
        parsed.get("error")
        or parsed.get("failure_class")
        or parsed.get("result_preview")
        or fallback
    )[:500]


def _environment_caveats(parsed: dict[str, Any]) -> tuple[str, ...]:
    issues = parsed.get("pre_existing_environment_issues")
    if not isinstance(issues, list) or not issues:
        return ()
    first = issues[0]
    if isinstance(first, dict):
        msg = str(first.get("message") or first.get("code") or "pre-existing environment issue")
    else:
        msg = str(first)
    return (f"Pre-existing environment issue: {msg}",)


def _not_applied_outcome(outcome: str) -> bool:
    return str(outcome).startswith("not_applied_") or str(outcome) == "failed_harness_error"


def _first_error(summary: str, extras: dict[str, Any] | None) -> str:
    if isinstance(extras, dict):
        errors = extras.get("errors")
        if isinstance(errors, list) and errors:
            return str(errors[0])
    first = summary.strip().splitlines()[0] if summary.strip() else ""
    return first[:500]
