from __future__ import annotations

import re
from enum import Enum


class DroneCommand(Enum):
    INSTALL = "install"
    REVISE = "revise"
    DISCARD = "discard"
    NEW = "new"
    LOAD = "load"
    HELP = "help"
    UNKNOWN = "unknown"


_INSTALL_PATTERNS = [
    r"^install\s*$",
    r"^install it\s*$",
    r"^install the drone\s*$",
    r"^register it\s*$",
    r"^register the drone\s*$",
]

_DISCARD_PATTERNS = [
    r"^discard\s*$",
    r"^discard it\s*$",
    r"^discard this drone\s*$",
    r"^throw away\s*$",
    r"^delete it\s*$",
    r"^delete this drone\s*$",
]

_NEW_PATTERNS = [
    r"^new\s*$",
    r"^new drone\s*$",
    r"^start over\s*$",
    r"^start new\s*$",
    r"^create new\s*$",
    r"^different drone\s*$",
    r"^scratch\s*$",
]

_LOAD_PATTERNS = [
    r"^load\s+(.+)$",
    r"^switch to\s+(.+)$",
]

_HELP_PATTERNS = [
    r"^help\s*$",
    r"^what can i say\s*$",
    r"^what can you do\s*$",
    r"^commands\??\s*$",
]

_DECISION_COMMAND_PHASES = {"awaiting_decision", "readiness_failed", "proof_failed"}


def parse_drone_command(text: str, phase: str) -> tuple[DroneCommand, str | None]:
    """Parse user text into a command and optional argument.

    Returns ``(DroneCommand, arg_or_None)``.
    """
    lowered = text.strip().lower()
    if not lowered:
        return DroneCommand.UNKNOWN, None

    # Global commands — checked first regardless of phase.
    for pattern in _NEW_PATTERNS:
        if re.search(pattern, lowered):
            return DroneCommand.NEW, None

    match = None
    for pattern in _LOAD_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            return DroneCommand.LOAD, match.group(1).strip()

    for pattern in _HELP_PATTERNS:
        if re.search(pattern, lowered):
            return DroneCommand.HELP, None

    # Phase-specific commands.
    if phase in _DECISION_COMMAND_PHASES:
        for pattern in _INSTALL_PATTERNS:
            if re.search(pattern, lowered):
                return DroneCommand.INSTALL, None

        for pattern in _DISCARD_PATTERNS:
            if re.search(pattern, lowered):
                return DroneCommand.DISCARD, None

        # In awaiting_decision, any non-command text is a revision.
        if phase == "awaiting_decision":
            return DroneCommand.REVISE, text.strip()

        # For readiness_failed / proof_failed, non-command text is unknown.
        return DroneCommand.UNKNOWN, None

    # In workshop, building, iterating:
    # No phase-specific commands beyond global ones.
    return DroneCommand.UNKNOWN, None
