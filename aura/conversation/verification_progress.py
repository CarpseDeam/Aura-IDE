"""Track repeated validation failure fingerprints and churn across worker edits."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_PYTEST_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_EXCEPTION_TYPE_RE = re.compile(
    r"^\s*([A-Za-z_]\w*(?:Error|Exception|Failure)):",
    re.MULTILINE,
)
_DURATION_RE = re.compile(r"\d+\.\d+s")
_HEX_ADDRESS_RE = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_TRACEBACK_LINE_RE = re.compile(r":\d+:")


class VerificationProgressTracker:
    """Detect validation runs that keep failing across worker edits."""

    def __init__(self, *, threshold: int = 3, churn_threshold: int = 6) -> None:
        self.threshold = threshold
        self.churn_threshold = churn_threshold
        self._failures: dict[str, tuple[frozenset[str], int]] = {}
        self._churn_failures: dict[str, int] = {}

    def observe(
        self,
        *,
        command: str,
        classification: str,
        output: str,
    ) -> dict[str, Any] | None:
        key = _command_key(command)

        if classification == "passed":
            self._failures.pop(key, None)
            self._churn_failures.pop(key, None)
            return None

        if classification != "product_validation_failed":
            return None

        fingerprint = fingerprint_failures(output)
        last_fingerprint, count = self._failures.get(key, (frozenset(), 0))
        count = count + 1 if fingerprint == last_fingerprint else 1
        self._failures[key] = (fingerprint, count)
        churn_count = self._churn_failures.get(key, 0) + 1
        self._churn_failures[key] = churn_count

        if count < self.threshold:
            if churn_count < self.churn_threshold:
                return None

            items = sorted(fingerprint)
            return {
                "ok": False,
                "recoverable": True,
                "phase_boundary": True,
                "reason": "verification_churn_budget_exceeded",
                "tool": "run_terminal_command",
                "message": (
                    "Verification churn budget exceeded: validation failed "
                    f"{churn_count} times for `{key}` without reaching green. "
                    "Stop calling tools and report completed work, current blocker, "
                    "latest failing items, and what changed across attempts so the "
                    "planner can choose a narrower repair or different approach: "
                    f"{_format_items(items)}."
                ),
                "verification_churn": {
                    "failed_runs": churn_count,
                    "threshold": self.churn_threshold,
                    "latest_fingerprint": items,
                },
            }

        items = sorted(fingerprint)
        return {
            "ok": False,
            "recoverable": True,
            "phase_boundary": True,
            "reason": "verification_not_converging",
            "tool": "run_terminal_command",
            "message": (
                "Verification is not converging: the same validation failures "
                f"repeated {count} times for `{key}`. Stop calling tools and "
                "report completed work, blockers, and these stuck failing items "
                f"so the planner can adjust the approach: {_format_items(items)}."
            ),
            "verification_stall": {
                "fingerprint": items,
                "repeated": count,
                "threshold": self.threshold,
            },
        }


def fingerprint_failures(output: str) -> frozenset[str]:
    text = str(output or "")

    pytest_failures = frozenset(match.group(1) for match in _PYTEST_FAILURE_RE.finditer(text))
    if pytest_failures:
        return pytest_failures

    exception_types = frozenset(
        f"{match.group(1)}:" for match in _EXCEPTION_TYPE_RE.finditer(text)
    )
    if exception_types:
        return exception_types

    normalized = _DURATION_RE.sub("<duration>", text)
    normalized = _HEX_ADDRESS_RE.sub("0xADDR", normalized)
    normalized = _TRACEBACK_LINE_RE.sub(":N:", normalized)
    normalized = " ".join(normalized.split())
    digest = hashlib.sha256(
        normalized.encode("utf-8", errors="surrogateescape")
    ).hexdigest()
    return frozenset({f"output:{digest}"})


def _command_key(command: str) -> str:
    return " ".join(str(command or "").split())


def _format_items(items: list[str]) -> str:
    if not items:
        return "<empty failure output>"
    if len(items) <= 12:
        return ", ".join(items)
    shown = ", ".join(items[:12])
    return f"{shown}, and {len(items) - 12} more"


__all__ = ["VerificationProgressTracker", "fingerprint_failures"]
