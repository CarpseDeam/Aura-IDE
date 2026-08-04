"""Emergency tool-call guardrails for conversation passes.

Normal control flow is handled by loop detection and the completion contract.
This module only keeps a high runaway guard so a broken model/tool loop cannot
run forever.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

RegistryMode = Literal["single", "planner", "worker"]

WRITE_TOOLS = {
    "write_file",
    "delete_file",
    "patch_file",
    "edit_godot_scene",
    "edit_godot_editor",
    "edit_godot_asset_preview",
    "install_godot_editor_bridge",
}
TERMINAL_TOOLS = {"run_terminal_command", "run_and_watch"}

# High emergency brake, not a workflow budget.
MAX_TOOL_CALLS_BY_MODE: dict[RegistryMode, int] = {
    "single": 300,
}


@dataclass
class ToolLimitState:
    """Tracks tool-call counts and enforces only high emergency totals."""

    mode: RegistryMode
    total_calls: int = 0
    terminal_calls: int = 0
    write_calls: int = 0

    def begin_model_round(self) -> None:
        """Reset per-round telemetry counters."""

    def check(self, tool_name: str) -> tuple[bool, dict[str, Any]]:
        """Return whether *tool_name* may run plus a JSON-ready reason payload.

        Only the high emergency total guard (runaway backstop) is enforced.
        """
        max_total = MAX_TOOL_CALLS_BY_MODE.get(self.mode, MAX_TOOL_CALLS_BY_MODE["single"])
        if self.total_calls + 1 > max_total:
            return False, self._payload(
                tool_name=tool_name,
                reason=f"{self.mode}_emergency_tool_call_limit_reached",
                limit_name="total_calls",
                limit=max_total,
                current=self.total_calls,
            )

        return True, {}

    def record(self, tool_name: str) -> None:
        """Record one accepted tool call for telemetry."""
        self.total_calls += 1
        if tool_name in TERMINAL_TOOLS:
            self.terminal_calls += 1
        if tool_name in WRITE_TOOLS:
            self.write_calls += 1

    def _payload(
        self,
        *,
        tool_name: str,
        reason: str,
        limit_name: str,
        limit: int,
        current: int,
        recoverable: bool = False,
        phase_boundary: bool = False,
        message: str | None = None,
    ) -> dict[str, Any]:
        if message is None:
            message = (
                "Emergency guard reached. Summarize completed work, current "
                "blockers, and the safest next step to continue."
            )
        return {
            "ok": False,
            "limit_reached": True,
            "recoverable": recoverable,
            "phase_boundary": phase_boundary,
            "reason": reason,
            "tool": tool_name,
            "limit_name": limit_name,
            "limit": limit,
            "current": current,
            "message": message,
            "counts": self.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total_calls": self.total_calls,
            "terminal_calls": self.terminal_calls,
            "write_calls": self.write_calls,
        }


def limit_reached_payload(info: dict[str, Any]) -> str:
    """Serialize a rejected tool-call payload."""
    return json.dumps(info, ensure_ascii=False)


__all__ = [
    "MAX_TOOL_CALLS_BY_MODE",
    "RegistryMode",
    "TERMINAL_TOOLS",
    "ToolLimitState",
    "WRITE_TOOLS",
    "limit_reached_payload",
]
