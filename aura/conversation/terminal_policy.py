"""Worker terminal command policy."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    normalized = " ".join(str(command or "").strip().lower().split())
    if not normalized:
        return "empty"
    return "terminal"


def worker_terminal_command_allowed(
    command: str,
    *,
    explicit_validation_commands: list[str] | None = None,
    workspace_root: Path | str | None = None,
) -> TerminalPolicyDecision:
    _ = workspace_root
    normalized = " ".join(str(command or "").strip().lower().split())
    explicit_commands = {
        " ".join(str(explicit or "").strip().lower().split())
        for explicit in explicit_validation_commands or []
    }
    if normalized and normalized in explicit_commands:
        return TerminalPolicyDecision(True, "worker terminal command allowed", "", "", "")
    if _looks_like_python_source_inspection(normalized):
        return TerminalPolicyDecision(
            allowed=False,
            reason=(
                "Worker terminal source inspection is blocked. Use read_file, "
                "read_file_range, or grep_search instead."
            ),
            failure_class="source_inspection_command_blocked",
            suggested_next_tool="read_file",
            suggested_next_action=(
                "Use read_file or read_file_range for source inspection, then run "
                "terminal commands only for validation or execution."
            ),
        )
    return TerminalPolicyDecision(True, "worker terminal command allowed", "", "", "")


def _looks_like_python_source_inspection(normalized_command: str) -> bool:
    if "python" not in normalized_command or " -c " not in normalized_command:
        return False
    source_read_markers = (
        ".read_text(",
        ".read_bytes(",
        "open(",
    )
    path_markers = (
        "path(",
        "pathlib",
    )
    return any(marker in normalized_command for marker in source_read_markers) and any(
        marker in normalized_command for marker in path_markers
    )


__all__ = [
    "TerminalPolicyDecision",
    "classify_worker_terminal_command",
    "worker_terminal_command_allowed",
]
