"""Derived command policy for detected validation commands.

This module owns *only* the policy rules that bridge detected project-level
validation commands (from ``ProjectProfile``) into the Worker's command list
and the behavioral-required enforcement in completion.

Rules:
* Deduplicate while preserving order.
* Normalize only enough for comparison and display.
* Exclude non-runnable placeholders such as ``python -m py_compile (touched files)``.
* Exclude pure parse/lint/static checks from the behavioral-required subset.
* Do not inspect the filesystem.
* Do not invent commands.
* Consume detected commands that already exist on ``ProjectProfile``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aura.conversation.project_profile import ProjectProfile

__all__ = [
    "behavioral_required_commands",
    "is_behavioral_required_command",
    "is_runnable_detected_validation_command",
    "merge_validation_commands",
    "normalize_command",
    "runnable_detected_validation_commands",
]

# ---------------------------------------------------------------------------
# Non-runnable patterns — placeholders that cannot be executed as-is
# ---------------------------------------------------------------------------

_NON_RUNNABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\(touched\s+files\)"),
)


def _is_non_runnable_placeholder(command: str) -> bool:
    """Return True when *command* is a non-runnable placeholder."""
    return any(p.search(command) for p in _NON_RUNNABLE_PATTERNS)


# ---------------------------------------------------------------------------
# Behavioral-required patterns — commands that must actually run to pass
# ---------------------------------------------------------------------------

# Normalized base commands that are behavioral (test / build / selfcheck).
_BEHAVIORAL_BASE_COMMANDS: frozenset[str] = frozenset({
    "pytest",
    "python -m pytest",
    "npm test",
    "npm run test",
    "npm run build",
    "cargo test",
    "cargo build",
    "go test",
    "go test ./...",
})

# Known non-behavioral (parse / lint / static-analysis) prefixes.
_NON_BEHAVIORAL_PREFIXES: tuple[str, ...] = (
    "python -m py_compile",
    "ruff",
    "mypy",
    "pyright",
    "pylint",
    "flake8",
    "black --check",
    "isort --check",
    "tsc --noEmit",
    "tsc",
)


def normalize_command(command: str) -> str:
    """Light normalization for command comparison: collapse whitespace, strip, lowercase."""
    return re.sub(r"\s+", " ", command).strip().lower()


def is_runnable_detected_validation_command(command: str) -> bool:
    """Return True when *command* is a runnable detected validation command.

    Excludes non-runnable placeholders such as ``python -m py_compile (touched files)``.
    """
    return not _is_non_runnable_placeholder(command)


def is_behavioral_required_command(command: str) -> bool:
    """Return True when *command* is a behavioral-required validation command.

    Behavioral commands (test / build / selfcheck) must be executed before the
    item is considered done.  Pure parse/lint/static checks are excluded.
    """
    if _is_non_runnable_placeholder(command):
        return False
    normalized = normalize_command(command)

    # Exact base-command match first.
    if normalized in _BEHAVIORAL_BASE_COMMANDS:
        return True

    # Prefix match for e.g. "python -m pytest tests/" or "cargo test --lib".
    for base in _BEHAVIORAL_BASE_COMMANDS:
        if normalized.startswith(base + " "):
            return True

    # Exclude known non-behavioral prefixes.
    for prefix in _NON_BEHAVIORAL_PREFIXES:
        if normalized.startswith(prefix):
            return False

    # No known behavioral pattern matched — exclude.
    return False


def behavioral_required_commands(commands: list[str]) -> list[str]:
    """Filter *commands* to only behavioral-required entries, order preserved."""
    return [c for c in commands if is_behavioral_required_command(c)]


def runnable_detected_validation_commands(
    profile: ProjectProfile,
) -> list[str]:
    """Extract runnable detected validation commands from *profile*.

    Filters out non-runnable placeholders.  Order is preserved from the
    profile's ``validation_commands`` tuple.
    """
    if not profile.validation_commands:
        return []
    return [
        cmd
        for cmd in profile.validation_commands
        if is_runnable_detected_validation_command(cmd)
    ]


def merge_validation_commands(
    planner_commands: list[str] | tuple[str, ...],
    detected_commands: list[str] | tuple[str, ...],
) -> list[str]:
    """Merge *planner_commands* and *detected_commands*, deduping while
    preserving order.

    Planner commands come first; detected commands fill in any gaps.
    """
    seen: set[str] = set()
    merged: list[str] = []

    for cmd in planner_commands:
        normal = normalize_command(cmd)
        if normal not in seen:
            seen.add(normal)
            merged.append(cmd)

    for cmd in detected_commands:
        normal = normalize_command(cmd)
        if normal not in seen:
            seen.add(normal)
            merged.append(cmd)

    return merged
