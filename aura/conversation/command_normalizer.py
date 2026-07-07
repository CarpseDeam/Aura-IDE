"""Command normalization — single surface for rewriting commands before execution.

This is the one normalization layer that every command execution path calls so
all harness-issued commands get consistent environment treatment regardless of
which code path issued them.

Currently handles:
- Python interpreter rewriting (``python``/``python3``/``py`` → project venv Python).
- Python module tool rewriting (``pytest``/``ruff``/``mypy`` → ``python -m tool``).
- Non-Python commands are left as-is.
- Shell-dialect validation (rejects bare ``cd``, leading ``export`` before execution).

Extend this module when adding new normalization rules; do not scatter
normalization logic across entry points.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from aura.project_env import build_project_command_rewrite


@dataclass(frozen=True)
class NormalizedCommand:
    command: str
    original_command: str
    normalized: bool
    normalization_reason: str = ""
    validation_error: str = ""

    @property
    def valid(self) -> bool:
        """True when the command passed shell-dialect validation."""
        return not self.validation_error


def normalize_command(command: str, workspace_root: Path) -> NormalizedCommand:
    """Normalize *command* for consistent execution environment.

    Rewrites Python interpreter and module-tool commands to use the project
    venv, then validates the result for shell-dialect correctness.

    Args:
        command: The raw command string to normalize.
        workspace_root: Root of the project workspace (used to detect
            project-local Python environments).

    Returns:
        A ``NormalizedCommand`` with the (potentially rewritten) command
        string, metadata about whether normalization occurred, and any
        shell-dialect validation error.
    """
    plan = build_project_command_rewrite(workspace_root, command)
    rewritten = plan.command
    error = _validate_command_shell(rewritten)
    return NormalizedCommand(
        command=rewritten,
        original_command=plan.original_command,
        normalized=rewritten != plan.original_command,
        validation_error=error,
    )


def _validate_command_shell(command: str) -> str:
    """Check *command* for ambiguous or unsupported shell constructs.

    Returns an error message if the command should be rejected, or an
    empty string if it appears well-formed.

    These checks are conservative — they catch patterns that are *always*
    wrong on the current platform or *always* semantically ambiguous,
    regardless of shell dialect.
    """
    stripped = command.strip()
    if not stripped:
        return ""

    # ---- Bare cd/chdir (no chained command) --------------------------------
    #  cd dir                   → meaningless in a subprocess
    #  chdir dir                → same
    #  cd dir && command        → OK (chained command, & is shell operator)
    #  cd dir; command          → OK (chained command)
    if _looks_like_bare_cd(stripped):
        return (
            "Ambiguous shell construct: bare 'cd' changes the working directory "
            "only for the current subprocess, which exits immediately. "
            "Use the structured 'cwd' / 'working_directory' parameter instead, "
            "or chain the command with '&&' (e.g. 'cd dir && command')."
        )

    # ---- Leading export (Unix shell construct) -----------------------------
    #  export VAR=value         → bash/zsh only, fails on Windows cmd
    #  export VAR=value && cmd  → same
    if _starts_with_export(stripped):
        return (
            "Unsupported shell construct: 'export' is a Unix shell feature "
            "not available on this platform. "
            "Set environment variables through the harness configuration instead."
        )

    return ""


def _looks_like_bare_cd(command: str) -> bool:
    """True when *command* is a bare ``cd`` or ``chdir`` without chained command."""
    # Check the first shell token.
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    first = tokens[0].strip("'\"").lower()
    if first not in {"cd", "chdir"}:
        return False
    # If there's a shell operator (&&, ||, ;, |), it's chained — not bare.
    remainder = command[len(tokens[0]):]
    for op in ("&&", "||", ";", "|"):
        if op in remainder:
            return False
    return True


def _starts_with_export(command: str) -> bool:
    """True when *command* starts with the ``export`` keyword."""
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    # "export" must be the very first token and followed by an assignment.
    if tokens[0].strip("'\"").lower() != "export":
        return False
    if len(tokens) < 2:
        return False
    # The second token must look like an assignment or variable name.
    second = tokens[1].strip("'\"")
    return "=" in second or (second.isidentifier() and second.isupper())


__all__ = [
    "NormalizedCommand",
    "normalize_command",
]
