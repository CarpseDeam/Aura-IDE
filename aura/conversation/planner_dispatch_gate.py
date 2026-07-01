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
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)|"
    r"i(?:['\u2019]?m)\s+(?:going\s+to\s+)?"
    r"(?:create|update|edit|modify|implement|refactor|extract|move|rename|"
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)|"
    r"let\s+me\s+"
    r"(?:create|update|edit|modify|implement|refactor|extract|move|rename|"
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)|"
    r"(?:next|now)\s+i\s*(?:will|['\u2019]?ll|can)\s+"
    r"(?:create|update|edit|modify|implement|refactor|extract|move|rename|"
    r"remove|delete|clean\s+up|cleanup|add|wire|patch|change|fix)"
    r")\b",
    re.IGNORECASE,
)

_IMPLEMENTATION_SURFACE_RE = re.compile(
    r"\b(?:implementation|refactor|cleanup|clean\s+up|module|file|function|"
    r"class|helper|prompt|tool|schema|registry|manager|router|policy)\b",
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
    dispatch_calls_seen: int,
    already_steered: bool,
) -> PlannerDispatchGateDecision:
    """Return an internal steering message when Planner must dispatch now."""
    if already_steered:
        return PlannerDispatchGateDecision(False)
    if planner_tool_calls_seen <= 0 or dispatch_calls_seen > 0:
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
            "implementation/refactor/cleanup work. You already inspected enough "
            "context to describe the bounded work, but your last assistant "
            "message narrated implementation intent instead of dispatching. "
            "Do not answer in chat. Do not call edit/write tools. Call "
            "dispatch_to_worker now with a self-contained Worker task capsule "
            "that names the goal, files, constraints, acceptance, and bounded "
            "steps when the work is non-trivial."
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
