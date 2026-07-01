"""Recognize when an action/task is completed and whether the model is producing
repetitive completion messages.
"""
from __future__ import annotations

import re
from typing import Any

from aura.conversation.dispatch import (
    WorkerDispatchResult,
    infer_outcome_status,
)
from aura.conversation.dispatch_lifecycle import is_internal_dispatch_continuation
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.conversation.worker_outcome import WorkerOutcomeStatus

COMPLETION_PHRASE_MARKERS = (
    "all set",
    "staged and ready",
    "ready for you",
    "let me know",
    "if you need anything else",
    "committed and done",
    "everything else is in good shape",
    "when you want to commit",
    "no further action needed",
)

TASK_COMPLETION_TOOL_NAMES = {
    "run_and_watch",
    "run_terminal_command",
    "run_diagnostic_command",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_log_file",
}

ACTION_COMPLETION_TOOL_NAMES = TASK_COMPLETION_TOOL_NAMES | WRITE_TOOLS


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""



def terminal_result_completed(info: dict[str, Any] | None) -> bool:
    payload = info.get("_terminal_payload") if isinstance(info, dict) else None
    return isinstance(payload, dict) and payload.get("exit_code") == 0


def tool_result_completes_action(name: str, ok: bool) -> bool:
    return ok and name in ACTION_COMPLETION_TOOL_NAMES


def completion_phrase_hits(text: str) -> set[str]:
    lowered = " ".join(str(text or "").lower().split())
    return {
        marker
        for marker in COMPLETION_PHRASE_MARKERS
        if marker in lowered
    }


def is_completion_style_message(text: str) -> bool:
    return bool(completion_phrase_hits(text))


def is_repetitive_completion_final(current: str, previous: str) -> bool:
    current_hits = completion_phrase_hits(current)
    previous_hits = completion_phrase_hits(previous)
    if current_hits and (current_hits & previous_hits):
        return True
    return text_overlap_ratio(current, previous) >= 0.7


def text_overlap_ratio(left: str, right: str) -> float:
    left_words = set(re.findall(r"[a-z0-9_]+", str(left).lower()))
    right_words = set(re.findall(r"[a-z0-9_]+", str(right).lower()))
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / max(len(left_words), len(right_words))


def worker_dispatch_is_terminal(result: WorkerDispatchResult | None) -> bool:
    """Return True if the Worker dispatch result is terminal — the Planner should not continue.

    Uses WorkerOutcomeStatus rather than duplicating status strings. Falls back
    to result.ok for unrecognized statuses.

    Internal continuations (campaign handback, planner handoff, recoverable
    spec-reject) are **non-terminal** — the Planner restarts internally.
    """
    if result is None:
        return False

    # Internal continuation is never terminal.
    if is_internal_dispatch_continuation(result):
        return False

    extras = result.extras if isinstance(result.extras, dict) else {}

    # Explicit boolean signals always win (fast path).
    if result.needs_followup:
        return False
    if result.recoverable:
        return False
    if result.phase_boundary:
        return False

    # Cancelled is always terminal.
    if result.cancelled:
        return True

    # Use canonical outcome status inference.
    status = infer_outcome_status(result)

    # Terminal statuses.
    if status in (
        WorkerOutcomeStatus.completed.value,
        WorkerOutcomeStatus.completed_with_caveats.value,
        WorkerOutcomeStatus.cancelled.value,
        WorkerOutcomeStatus.approval_rejected.value,
    ):
        return True

    # harness_error is terminal when not recoverable/needs_followup/phase_boundary
    # (those booleans already filtered above).
    if status == WorkerOutcomeStatus.harness_error.value:
        return True

    # Non-terminal statuses — these need Planner attention.
    if status in (
        WorkerOutcomeStatus.needs_followup.value,
        WorkerOutcomeStatus.needs_planner_resolution.value,
        WorkerOutcomeStatus.validation_failed.value,
        WorkerOutcomeStatus.edit_mechanics_blocked.value,
        WorkerOutcomeStatus.scope_mismatch.value,
    ):
        return False

    # Fallback: rely on result.ok for unknown/unrecognized statuses.
    return bool(result.ok)
