"""Pure terminal/run policy helpers extracted from ToolRunner."""
from __future__ import annotations

import re
from typing import Any

from aura.conversation.validation_orchestrator import parse_validation_command
from aura.work_artifact.model import ValidationCommandSpec


DEFAULT_TERMINAL_TIMEOUT_SECONDS = 300
DEFAULT_PY_COMPILE_TIMEOUT_SECONDS = 30
MAX_TERMINAL_TIMEOUT_SECONDS = 300

_CD_WRAPPER_RE = re.compile(
    r'^(?:cd|chdir)\s+(?:"/workspace"|\'/workspace\'|/workspace)\s*(?:&&|;)\s*',
    re.IGNORECASE,
)


def is_py_compile_command(command: str) -> bool:
    """Check whether *command* is a ``python -m py_compile`` invocation."""
    normalized = " ".join(command.strip().lower().split())
    return " -m py_compile" in normalized or "python -m py_compile" in normalized


def matches_explicit_validation(
    command: str,
    explicit_validation_commands: list[ValidationCommandSpec] | list[str] | None,
    *,
    cwd: str = "",
) -> bool:
    """Return True when *command* is identical to an explicit validation command.

    Accepts both structured ``ValidationCommandSpec`` entries (preferred) and
    legacy flat strings for backward compatibility.
    """
    normalized = " ".join(str(command or "").strip().lower().split())
    normalized_cwd = _normalize_cwd(cwd)
    for entry in explicit_validation_commands or []:
        if isinstance(entry, ValidationCommandSpec):
            spec_command = entry.command
            spec_cwd = entry.cwd
        else:
            parsed = parse_validation_command(str(entry), source="explicit_task_command")
            spec_command = parsed.command
            spec_cwd = parsed.cwd
        if normalized == " ".join(spec_command.strip().lower().split()) and normalized_cwd == _normalize_cwd(spec_cwd):
            return True
    return False


def resolve_terminal_timeout(command: str, timeout_arg: Any, /) -> int:
    """Resolve the timeout (seconds) for a terminal command.

    Returns the appropriate default based on the command type, or the
    user-provided *timeout_arg* clamped to ``MAX_TERMINAL_TIMEOUT_SECONDS``.
    """
    if timeout_arg is None:
        if is_py_compile_command(command):
            return DEFAULT_PY_COMPILE_TIMEOUT_SECONDS
        return DEFAULT_TERMINAL_TIMEOUT_SECONDS

    try:
        timeout = int(timeout_arg)
    except (TypeError, ValueError):
        if is_py_compile_command(command):
            return DEFAULT_PY_COMPILE_TIMEOUT_SECONDS
        return DEFAULT_TERMINAL_TIMEOUT_SECONDS

    return max(1, min(timeout, MAX_TERMINAL_TIMEOUT_SECONDS))


def _normalize_cwd(cwd: str) -> str:
    return str(cwd or "").strip().replace("\\", "/").strip("/")
