"""Validation-classification helpers for WorkerEventRelay.

Self-contained functions that classify terminal-command records as
validation attempts and attach metadata for completion evidence.
"""

from __future__ import annotations

import re
from typing import Any

from aura.conversation.validation_orchestrator import classify_validation_payload, looks_like_validation_command


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


def _is_nonfatal_terminal_record(record: dict[str, Any]) -> bool:
    return (
        record.get("terminal_command_role") == "inspection_search"
        and record.get("terminal_classification") == "search_no_match"
        and record.get("terminal_no_matches") is True
    )


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


def _attach_validation_metadata(record: dict[str, Any], parsed: dict[str, Any]) -> None:
    should_classify = (
        bool(parsed.get("auto_validation"))
        or bool(parsed.get("validation_source"))
        or bool(parsed.get("validation_raw_text"))
        or bool(parsed.get("classification"))
        or bool(parsed.get("validation_classification"))
    )
    if not should_classify:
        # Fallback: known validation commands (compileall, pytest, ruff, mypy,
        # etc.) get classified even when the tool runner omitted explicit
        # metadata fields.  Synthesize the missing fields from what we have
        # so classify_validation_payload produces a meaningful result.
        command = str(parsed.get("command") or record.get("command") or "")
        if command and looks_like_validation_command(command):
            should_classify = True
            parsed = dict(parsed)
            parsed.setdefault("validation_raw_text", command)
            parsed.setdefault("validation_source", "terminal_known_validation")
    if not should_classify:
        return
    run = classify_validation_payload(parsed)
    record.update(run.metadata())
