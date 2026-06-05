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
    return TerminalPolicyDecision(True, "worker terminal command allowed", "", "", "")


__all__ = [
    "TerminalPolicyDecision",
    "classify_worker_terminal_command",
    "worker_terminal_command_allowed",
]
