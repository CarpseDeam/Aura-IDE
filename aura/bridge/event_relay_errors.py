"""Validation-classification helpers for WorkerEventRelay.

Self-contained functions that classify terminal-command records as
validation attempts.  No authority is derived from these classifications.
"""

from __future__ import annotations

import re
from typing import Any


def _is_validation_terminal_record(record: dict[str, Any]) -> bool:
    if "counts_as_validation" in record:
        return bool(record.get("counts_as_validation"))
    if record.get("auto_validation"):
        return True
    command = str(record.get("command") or "").strip()
    if not command:
        return False
    normalized = " ".join(command.lower().split())
    python_exe = r"(?:(?:\"[^\"]*python3?(?:\.exe)?\")|(?:'[^']*python3?(?:\.exe)?')|\S*python3?(?:\.exe)?|py)"

    known_patterns = (
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+py_compile\b",
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+compileall\b",
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+(?:pytest|unittest|ruff|mypy)\b",
        r"(^|[;&|]\s*)pytest\b",
        r"(^|[;&|]\s*)unittest\b",
        r"(^|[;&|]\s*)ruff\s+(?:check|format\s+--check)\b",
        r"(^|[;&|]\s*)mypy\b",
        r"(^|[;&|]\s*)npm\s+(?:test|run\s+(?:test|build))\b",
        r"(^|[;&|]\s*)cargo\s+(?:test|build)\b",
        r"(^|[;&|]\s*)go\s+test\b",
    )
    if any(re.search(pattern, normalized) for pattern in known_patterns):
        return True
    if _is_python_assertion_command(normalized):
        return True
    if _is_search_command_with_explicit_shell_assertion(normalized):
        return True
    return False


def _is_python_assertion_command(normalized_command: str) -> bool:
    python_exe = r"(?:(?:\"[^\"]*python3?(?:\.exe)?\")|(?:'[^']*python3?(?:\.exe)?')|\S*python3?(?:\.exe)?|py)"
    if not re.search(rf"(^|[;&|]\s*){python_exe}\s+-c\s+", normalized_command):
        return False
    return any(token in normalized_command for token in ("assert ", "raise systemexit", "sys.exit("))


def _is_search_command_with_explicit_shell_assertion(normalized_command: str) -> bool:
    if not re.search(r"^\s*(?:rg|grep|findstr)\b", normalized_command):
        return False
    return bool(
        re.search(r"&&\s*exit\s+1\s*\|\|\s*exit\s+0\b", normalized_command)
        or re.search(r"\|\|\s*exit\s+1\b", normalized_command)
    )
