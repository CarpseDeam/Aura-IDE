"""Summary formatting helpers — extracted from worker_report.py."""

from __future__ import annotations

import json
import re
from typing import Any


def _final_report_claims_failure(content: str) -> bool:
    text = content.lower()
    if re.search(r"\bno\s+(?:blocker|blockers|blocked)\b", text):
        text = re.sub(r"\bno\s+(?:blocker|blockers|blocked)\b", "", text)
    return any(
        re.search(pattern, text)
        for pattern in (
            r"\bblocker(?:s)?\b",
            r"\bblocked\b",
            r"\bfailed\s+validation\b",
            r"\bvalidation\s+failed\b",
            r"\bfailed\s+acceptance\b",
            r"\bacceptance\s+failed\b",
            r"\bcould\s+not\s+verify\b",
            r"\bcouldn't\s+verify\b",
            r"\bcannot\s+verify\b",
            r"\bunable\s+to\s+verify\b",
            r"\bnot\s+verified\b",
            r"\bcould\s+not\s+run\b",
            r"\bcouldn't\s+run\b",
            r"\bunable\s+to\s+run\b",
            r"\btests?\s+failed\b",
            r"\bpytest\s+failed\b",
            r"\blint\s+failed\b",
        )
    )


def _final_report_claims_validation(content: str) -> bool:
    text = content.lower()
    if re.search(r"\bnot\s+(?:tested|validated|verified)\b", text):
        text = re.sub(r"\bnot\s+(?:tested|validated|verified)\b", "", text)
    return any(
        re.search(pattern, text)
        for pattern in (
            r"\bverified\b",
            r"\bvalidated\b",
            r"\bpytest\b",
            r"\bpy_compile\b",
            r"\bruff\b",
            r"\bmypy\b",
            r"\btests?\s+pass(?:ed|es)?\b",
            r"\bcompiled\b",
            r"\bexit\s+code\s+0\b",
            r"\bexits\s+0\b",
        )
    )


def _parse_structured_worker_failure(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # Recognize mismatch_detected as structured metadata, not a failure.
    if parsed.get("status") == "mismatch_detected" and isinstance(parsed.get("mismatch"), dict):
        return parsed
    if parsed.get("ok") is not False:
        return {}
    failure_class = parsed.get("failure_class")
    error = parsed.get("error")
    if not failure_class or not error:
        return {}
    return parsed


def _format_structured_worker_failure(result: dict[str, Any]) -> str:
    error = str(result.get("error") or "Harness error.")
    failure_class = str(result.get("failure_class") or "worker_failed")
    detail = result.get("details")
    if isinstance(detail, dict) and detail:
        path = str(detail.get("path") or "")
        tool = str(detail.get("tool") or "")
        reason = str(detail.get("reason") or detail.get("failure_class") or "")
        op = detail.get("failed_operation")
        op_text = ""
        if isinstance(op, dict) and op:
            op_text = f" Failed operation: {json.dumps(op, ensure_ascii=False, sort_keys=True)}."
        target = f" Path: {path}." if path else ""
        tool_text = f" Tool: {tool}." if tool else ""
        reason_text = f" Reason: {reason}." if reason else ""
        return f"{error} ({failure_class}).{target}{tool_text}{reason_text}{op_text}"
    return f"{error} ({failure_class})."


def _format_worker_write_failure(result: dict[str, Any]) -> str:
    name = str(result.get("name") or "write_tool")
    path = str(result.get("path") or "")
    error = str(result.get("error") or result.get("result_preview") or "unknown error")
    failure_class = str(result.get("failure_class") or "internal_error")
    target = f" on {path}" if path else ""
    return f"Write tool '{name}' failed{target}: {error} ({failure_class})."


def _format_recoverable_write_failure(result: dict[str, Any]) -> str:
    name = str(result.get("name") or "write_tool")
    path = str(result.get("path") or "")
    error = str(result.get("error") or result.get("result_preview") or "recoverable edit mechanics failure")
    suggested = str(result.get("suggested_next_tool") or result.get("suggested_tool") or "patch_file")
    target = f" on {path}" if path else ""
    op = result.get("failed_operation")
    op_text = ""
    if isinstance(op, dict) and op:
        op_text = f" Failed operation: {json.dumps(op, ensure_ascii=False, sort_keys=True)}."
    return f"Recoverable edit mechanics failure from {name}{target}: {error}. Next tactic: {suggested}.{op_text}"
