"""Planner completion guard for implementation work.

This catches the specific failure mode where the Planner has inspected local
context, then ends the turn with visible implementation narration instead of
calling dispatch_to_worker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aura.conversation.completion_guard import assistant_message_text
from aura.conversation.task_router import TaskLane, classify_user_request
from aura.research.policy import ANSWER_ONLY, decide_research_policy

_IMPLEMENTATION_NARRATION_RE = re.compile(
    r"\b(?:"
    r"i\s*(?:will|['\u2019]?ll|am going to|can|would)\s+"
    r"(?:create|update|edit|modify|implement|refactor|extract|move|rename|"
    r"write|writes|writing|written|"
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)|"
    r"i(?:['\u2019]?m)\s+(?:going\s+to\s+)?"
    r"(?:create|update|edit|modify|implement|refactor|extract|move|rename|"
    r"write|writes|writing|written|"
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)|"
    r"let\s+me\s+"
    r"(?:create|update|edit|modify|implement|refactor|extract|move|rename|"
    r"write|writes|writing|written|"
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)|"
    r"(?:next|now)\s+i\s*(?:will|['\u2019]?ll|can)\s+"
    r"(?:create|update|edit|modify|implement|refactor|extract|move|rename|"
    r"write|writes|writing|written|"
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)"
    r")\b",
    re.IGNORECASE,
)

_IMPLEMENTATION_SURFACE_RE = re.compile(
    r"\b(?:implementation|refactor|cleanup|clean\s+up|module|file|function|"
    r"class|helper|prompt|tool|schema|registry|manager|router|policy|test|tests|"
    r"test\s+file|test\s+files|regression\s+test|regression\s+tests|"
    r"test\s+coverage)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlannerDispatchGateDecision:
    should_continue: bool
    steering_message: str = ""
    reason: str = ""


def maybe_force_worker_dispatch(
    *,
    latest_user_text: str,
    candidate_message: dict[str, Any],
    planner_tool_calls_seen: int,
    dispatch_calls_seen: int = 0,
    dispatch_accepted: bool | None = None,
    dispatch_recovery_required: bool = False,
    dispatch_recovery_message: str = "",
    already_steered: bool = False,
) -> PlannerDispatchGateDecision:
    """Return an internal steering message when Planner must dispatch now."""
    accepted = dispatch_calls_seen > 0 if dispatch_accepted is None else dispatch_accepted
    if accepted:
        return PlannerDispatchGateDecision(False)
    if dispatch_recovery_required:
        return PlannerDispatchGateDecision(
            True,
            steering_message=(
                dispatch_recovery_message
                or "INTERNAL PLANNER DISPATCH RECOVERY: The previous dispatch was "
                "not accepted and no Worker started. Correct dispatch_to_worker and "
                "call it again now; an empty or narration-only response cannot end this turn."
            ),
            reason="planner_dispatch_recovery_required",
        )
    if already_steered:
        return PlannerDispatchGateDecision(False)
    if not _latest_user_is_local_implementation(latest_user_text):
        return PlannerDispatchGateDecision(False)

    text = assistant_message_text(candidate_message).strip()
    if not text:
        return PlannerDispatchGateDecision(False)
    if _looks_like_user_owned_question_or_blocker(text):
        return PlannerDispatchGateDecision(False)
    if not _looks_like_implementation_narration(text):
        return PlannerDispatchGateDecision(False)

    return PlannerDispatchGateDecision(
        True,
        steering_message=(
            "INTERNAL PLANNER DISPATCH GATE: The latest user request is local "
            "implementation/test/refactor work. Your last response narrated "
            "implementation intent instead of calling dispatch_to_worker. Do "
            "not answer in chat. Do not call edit/write tools. If enough target "
            "context is already known, call dispatch_to_worker now. If one short "
            "inspection pass is required to name target files, use read-only "
            "inspection only, then dispatch. This turn must not end without "
            "dispatch_to_worker unless asking one real user-owned blocker "
            "question."
        ),
        reason="planner_implementation_narration_without_dispatch",
    )


def _latest_user_is_local_implementation(text: str) -> bool:
    policy = decide_research_policy(text)
    if policy.route == ANSWER_ONLY:
        return False
    route = classify_user_request(text)
    return route.lane == TaskLane.implementation


def _looks_like_implementation_narration(text: str) -> bool:
    normalized = " ".join(str(text or "").split())
    return bool(
        _IMPLEMENTATION_NARRATION_RE.search(normalized)
        and _IMPLEMENTATION_SURFACE_RE.search(normalized)
    )


def _looks_like_user_owned_question_or_blocker(text: str) -> bool:
    stripped = str(text or "").strip()
    lowered = stripped.lower()
    if "?" in stripped:
        return not _looks_like_implementation_narration(stripped)
    return lowered.startswith(
        (
            "blocked",
            "i need you to",
            "please provide",
            "which ",
            "choose ",
            "i need a decision",
        )
    )


__all__ = ["PlannerDispatchGateDecision", "maybe_force_worker_dispatch"]
