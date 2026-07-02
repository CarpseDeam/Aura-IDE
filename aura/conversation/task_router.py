"""Deterministic routing for user requests before model dispatch."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from aura.research.policy import (
    ANSWER_ONLY,
    RESEARCH_THEN_WORKER,
    decide_research_policy,
)


class TaskLane(str, Enum):
    built_in_action = "built_in_action"
    implementation = "implementation"
    validation = "validation"
    research = "research"
    chat = "chat"


@dataclass(frozen=True)
class TaskRoute:
    lane: TaskLane
    action: str
    confidence: float
    reason: str


def classify_user_request(text: str) -> TaskRoute:
    """Classify a user request into the lane that should handle it."""
    raw = str(text or "").strip()
    normalized = _normalize(raw)
    if not normalized:
        return TaskRoute(TaskLane.chat, "chat", 0.4, "empty request")

    built_in_action = _classify_built_in(normalized)
    if built_in_action:
        return TaskRoute(
            TaskLane.built_in_action,
            built_in_action,
            1.0,
            "matched built-in action",
        )

    if _looks_like_validation(normalized):
        return TaskRoute(
            TaskLane.validation,
            "validation",
            0.9,
            "matched validation command/request",
        )

    research_policy = decide_research_policy(raw)
    if research_policy.route in {ANSWER_ONLY, RESEARCH_THEN_WORKER}:
        return TaskRoute(
            TaskLane.research,
            "web_research"
            if research_policy.route == ANSWER_ONLY
            else RESEARCH_THEN_WORKER,
            research_policy.intent.confidence,
            research_policy.reason,
        )

    if _looks_like_implementation(normalized):
        return TaskRoute(
            TaskLane.implementation,
            "implementation",
            0.85,
            "matched implementation/change request",
        )

    return TaskRoute(TaskLane.chat, "chat", 0.6, "no task-lane trigger matched")


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _classify_built_in(normalized: str) -> str | None:
    if normalized == "/undo":
        return "undo"
    if re.search(r"\bundo\b.*\b(?:last|most recent)\b.*\bcommit\b", normalized):
        return "undo"
    if "reset --soft" in normalized or "soft reset" in normalized:
        return "undo"

    if normalized in {
        "git status",
        "show git status",
        "what is git status",
        "current git status",
    }:
        return "git_status"
    if (
        normalized == "git diff"
        or normalized.startswith("git diff ")
        or normalized == "show git diff"
    ):
        return "git_diff"
    if (
        normalized == "git log"
        or normalized.startswith("git log ")
        or normalized == "show git log"
    ):
        return "git_log"
    if re.search(r"\brestore\b.*\bsnapshot\b", normalized):
        return "restore_snapshot"
    if normalized == "/drone":
        return "drone_enter_mode"
    return None


def _looks_like_validation(normalized: str) -> bool:
    validation_patterns = (
        r"^(?:run|execute)?\s*(?:python|py)\s+-m\s+py_compile\b",
        r"^(?:run|execute)?\s*(?:python|py)\s+-m\s+(?:pytest|unittest)\b",
        r"^(?:run|execute)?\s*pytest\b",
        r"^(?:run|execute)?\s*ruff\s+(?:check|format\s+--check)\b",
        r"^(?:run|execute)?\s*mypy\b",
        r"^(?:run|execute)?\s*npm\s+(?:test|run\s+(?:test|build))\b",
        r"^(?:run|execute)?\s*cargo\s+(?:test|build)\b",
        r"^(?:run|execute)?\s*go\s+test\b",
        r"^(?:run|execute)\s+(?:the\s+)?(?:tests|test suite|build|validation)\b",
    )
    return any(re.search(pattern, normalized) for pattern in validation_patterns)


def _looks_like_implementation(normalized: str) -> bool:
    return bool(
        re.search(
            r"\b(?:add|build|change|clean\s+up|cleanup|create|delete|extract|fix|"
            r"implement|modify|move|refactor|remove|rename|repair|update|wire)\b",
            normalized,
        )
        or re.search(
            r"\b(?:write|writes|writing|written)\b.*\b(?:tests?|test\s+files?|"
            r"regression\s+tests?|test\s+coverage)\b",
            normalized,
        )
    )


__all__ = ["TaskLane", "TaskRoute", "classify_user_request"]
