"""Worker terminal command policy."""
from __future__ import annotations

from dataclasses import dataclass
import re
import shlex

SOURCE_INSPECTION_ERROR = (
    "Worker terminal is validation-only. Use structured read tools for source inspection."
)
SOURCE_INSPECTION_NEXT_ACTION = (
    "Use read_file, read_files, grep_search, read_file_outline, find_usages, or "
    "search_codebase. If structured reads cannot access the file, report a blocker "
    "instead of trying terminal/Python file reads."
)


@dataclass(frozen=True)
class TerminalPolicyDecision:
    allowed: bool
    reason: str
    failure_class: str
    suggested_next_tool: str
    suggested_next_action: str

    def to_blocked_payload(self, command: str) -> dict[str, object]:
        return {
            "ok": False,
            "failure_class": self.failure_class,
            "error": self.reason,
            "recoverable": True,
            "suggested_next_tool": self.suggested_next_tool,
            "suggested_next_action": self.suggested_next_action,
            "blocked_command": command,
        }


def classify_worker_terminal_command(command: str) -> str:
    """Classify a Worker terminal command as validation, source inspection, or unknown."""
    normalized = _normalize_command(command)
    if not normalized:
        return "unknown"
    if _looks_like_source_inspection(normalized):
        return "source_inspection"
    if _looks_like_validation(normalized):
        return "validation"
    return "unknown"


def worker_terminal_command_allowed(
    command: str,
    *,
    explicit_validation_commands: list[str] | None = None,
) -> TerminalPolicyDecision:
    """Return whether a normal Worker may run *command* in terminal."""
    if _matches_explicit_validation(command, explicit_validation_commands):
        return TerminalPolicyDecision(True, "explicit validation command", "", "", "")

    classification = classify_worker_terminal_command(command)
    if classification == "validation":
        return TerminalPolicyDecision(True, "validation command", "", "", "")
    if classification == "source_inspection":
        return TerminalPolicyDecision(
            False,
            SOURCE_INSPECTION_ERROR,
            "source_inspection_command_blocked",
            "read_file",
            SOURCE_INSPECTION_NEXT_ACTION,
        )

    return TerminalPolicyDecision(
        False,
        "Worker terminal is validation-only. This command is not a recognized validation/build/test command.",
        "worker_terminal_not_validation",
        "run_terminal_command",
        "Run only validation/build/test commands, or use an explicit validation command from the Worker handoff.",
    )


def _normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().lower().split())


def _matches_explicit_validation(
    command: str,
    explicit_validation_commands: list[str] | None,
) -> bool:
    normalized = _normalize_command(command)
    if not normalized:
        return False
    for explicit in explicit_validation_commands or []:
        if normalized == _normalize_command(explicit):
            return True
    return False


def _looks_like_validation(normalized: str) -> bool:
    segments = _split_command_segments(normalized)
    if not segments:
        return False
    return all(_segment_looks_like_validation(segment) for segment in segments)


def _segment_looks_like_validation(segment: str) -> bool:
    validation_patterns = (
        r"^(?:python(?:\d+(?:\.\d+)?)?|py)\s+-m\s+py_compile\b",
        r"^(?:python(?:\d+(?:\.\d+)?)?|py)\s+-m\s+(?:pytest|unittest)\b",
        r"^pytest\b",
        r"^unittest\b",
        r"^ruff\s+(?:check|format\s+--check)\b",
        r"^mypy\b",
        r"^npm\s+(?:test|run\s+(?:test|build))\b",
        r"^cargo\s+(?:test|build)\b",
        r"^go\s+test\b",
    )
    return any(re.search(pattern, segment) for pattern in validation_patterns)


def _looks_like_source_inspection(normalized: str) -> bool:
    if _python_reads_source(normalized):
        return True
    for segment in _split_command_segments(normalized):
        if _segment_reads_source(segment):
            return True
    return False


def _python_reads_source(normalized: str) -> bool:
    if not re.search(r"(^|[;&|]\s*)(?:python(?:\d+(?:\.\d+)?)?|py)\s+-c\s+", normalized):
        return False
    read_markers = (
        ".read_text(",
        ".read_bytes(",
        ".readlines(",
        ".read(",
        "pathlib",
        "from pathlib import path",
        "open(",
        ".read()",
        ".readlines()",
        "linecache",
    )
    return any(marker in normalized for marker in read_markers)


def _split_command_segments(normalized: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(r"\s*(?:&&|\|\||[;|])\s*", normalized)
        if segment.strip()
    ]


def _segment_reads_source(segment: str) -> bool:
    if _powershell_reads_source(segment):
        return True
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return False

    executable = tokens[0].strip("'\"").replace("\\", "/").rsplit("/", 1)[-1]
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in {"cat", "type", "gc", "get-content", "head", "tail"}:
        return True
    if executable in {"rg", "grep", "findstr"}:
        return True
    if executable == "sed" and len(tokens) > 1 and tokens[1].strip("'\"").startswith("-n"):
        return True
    if executable == "awk" and _awk_prints_source(tokens[1:]):
        return True
    return False


def _powershell_reads_source(segment: str) -> bool:
    if not re.match(r"^(?:powershell|pwsh)(?:\.exe)?\b", segment):
        return False
    return bool(
        re.search(r"\b(?:get-content|gc|type|cat)\b", segment)
        or re.search(r"\[(?:system\.)?io\.file\]::read(?:all)?(?:text|lines|bytes)\b", segment)
    )


def _awk_prints_source(args: list[str]) -> bool:
    joined = " ".join(args)
    if "print" not in joined:
        return False
    return bool(re.search(r"\.(?:py|js|ts|tsx|jsx|go|rs|java|cs|cpp|c|h|hpp|md|toml|yaml|yml|json)\b", joined))


__all__ = [
    "classify_worker_terminal_command",
    "TerminalPolicyDecision",
    "worker_terminal_command_allowed",
]
