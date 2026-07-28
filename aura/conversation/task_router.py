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

_BUILT_IN_MAX_WORDS = 10
_POLITE_PREFIX = re.compile(
    r"^(?:please|pls|hey|ok|okay|now|just|aura|can you|could you|would you)\b[,:]?\s+"
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

    built_in_action = _classify_built_in(raw, normalized)
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


def _classify_built_in(raw: str, normalized: str) -> str | None:
    if normalized == "/undo":
        return "undo"
    if normalized == "/drone":
        return "drone_enter_mode"

    # Everything below is a phrase match. Only let those fire on short,
    # single-line messages: a long work request that happens to mention
    # "restore" and "snapshot" is a task for the model, not a git command.
    if not _is_command_like(raw, normalized):
        return None

    command = _strip_polite_prefix(normalized)

    if re.match(r"undo\b(?:\s+\w+){0,3}\s+(?:last|most recent)\s+commit\b", command):
        return "undo"
    if command.startswith(("git reset --soft", "reset --soft", "soft reset")):
        return "undo"

    if command in {
        "git status",
        "show git status",
        "what is git status",
        "current git status",
    }:
        return "git_status"
    if (
        command == "git diff"
        or command.startswith("git diff ")
        or command == "show git diff"
    ):
        return "git_diff"
    if (
        command == "git log"
        or command.startswith("git log ")
        or command == "show git log"
    ):
        return "git_log"
    if re.match(r"restore\b(?:\s+\w+){0,3}\s+snapshot\b", command):
        return "restore_snapshot"
    return None


def _is_command_like(raw: str, normalized: str) -> bool:
    """True when the message is short enough to be a literal command.

    Built-in actions bypass the model entirely and drop the request, so a
    false positive silently swallows real work. Multi-line prompts and
    anything longer than a terse command never qualify.
    """
    if "\n" in raw.strip():
        return False
    return len(normalized.split()) <= _BUILT_IN_MAX_WORDS


def _strip_polite_prefix(normalized: str) -> str:
    previous = ""
    command = normalized
    while command != previous:
        previous = command
        command = _POLITE_PREFIX.sub("", command).strip()
    return command


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
