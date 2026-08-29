"""Recognition of Aura's own literal chat commands.

This is a command palette, not a task classifier. It answers one question —
"did the user type one of Aura's own commands?" — for a short, literal message
like ``/undo`` or ``git status``, so the GUI can run it locally instead of
sending it to the model. Anything it does not recognise is an ordinary message
and goes to the production loop untouched; nothing here inspects, shapes, or
constrains what the model may then do.
"""
from __future__ import annotations

import re

_BUILT_IN_MAX_WORDS = 10
_POLITE_PREFIX = re.compile(
    r"^(?:please|pls|hey|ok|okay|now|just|aura|can you|could you|would you)\b[,:]?\s+"
)


def classify_built_in_command(text: str) -> str | None:
    """Return the built-in command *text* literally is, or ``None``.

    ``None`` is the normal answer: an ordinary request is not a command.
    """
    raw = str(text or "").strip()
    normalized = " ".join(raw.lower().split())
    if not normalized:
        return None

    if normalized == "/undo":
        return "undo"
    if normalized == "/agents":
        return "agents_enter_mode"
    # Literal only: a message that merely talks about skills is a real request.
    if normalized == "/skills":
        return "skills"

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

    Built-in commands bypass the model entirely and drop the request, so a
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


__all__ = ["classify_built_in_command"]
