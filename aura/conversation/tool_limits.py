"""Simple count-based tool limits for conversation passes."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

RegistryMode = Literal["single", "planner", "worker", "researcher"]

WRITE_TOOLS = {"write_file", "edit_file", "edit_symbol"}
TERMINAL_TOOLS = {"run_terminal_command"}
DISPATCH_TOOLS = {"dispatch_to_worker"}
RESEARCH_TOOLS = {"run_research"}

MAX_TOOL_CALLS_BY_MODE: dict[RegistryMode, int] = {
    "planner": 40,
    "worker": 100,
    "single": 80,
    "researcher": 20,
}

MAX_WORKER_REDISPATCHES_PER_USER_TURN = 2
MAX_TERMINAL_CALLS_PER_WORKER_PASS = 10
MAX_WRITE_CALLS_PER_WORKER_PASS = 30
MAX_DISPATCH_CALLS_PER_PLANNER_TURN = 1
MAX_RESEARCH_CALLS_PER_PLANNER_TURN = 1


@dataclass
class ToolLimitState:
    """Tracks count-based tool limits for one conversation pass."""

    mode: RegistryMode
    total_calls: int = 0
    terminal_calls: int = 0
    write_calls: int = 0
    dispatch_calls: int = 0
    research_calls: int = 0
    round_dispatch_calls: int = 0
    round_research_calls: int = 0

    def begin_model_round(self) -> None:
        """Reset caps that apply to one planner model round."""
        self.round_dispatch_calls = 0
        self.round_research_calls = 0

    def check(self, tool_name: str) -> tuple[bool, dict[str, Any]]:
        """Return whether *tool_name* may run plus a JSON-ready reason payload."""
        max_total = MAX_TOOL_CALLS_BY_MODE.get(self.mode, MAX_TOOL_CALLS_BY_MODE["single"])
        if self.total_calls + 1 > max_total:
            return False, self._payload(
                tool_name=tool_name,
                reason=f"{self.mode}_tool_call_limit_reached",
                limit_name="total_calls",
                limit=max_total,
                current=self.total_calls,
                recoverable=self.mode == "worker",
                phase_boundary=self.mode == "worker",
            )

        if self.mode == "worker" and tool_name in TERMINAL_TOOLS:
            if self.terminal_calls + 1 > MAX_TERMINAL_CALLS_PER_WORKER_PASS:
                return False, self._payload(
                    tool_name=tool_name,
                    reason="worker_terminal_call_limit_reached",
                    limit_name="terminal_calls",
                    limit=MAX_TERMINAL_CALLS_PER_WORKER_PASS,
                    current=self.terminal_calls,
                )

        if self.mode == "worker" and tool_name in WRITE_TOOLS:
            if self.write_calls + 1 > MAX_WRITE_CALLS_PER_WORKER_PASS:
                return False, self._payload(
                    tool_name=tool_name,
                    reason="worker_write_call_limit_reached",
                    limit_name="write_calls",
                    limit=MAX_WRITE_CALLS_PER_WORKER_PASS,
                    current=self.write_calls,
                )

        if self.mode == "planner" and tool_name in DISPATCH_TOOLS:
            if self.round_dispatch_calls + 1 > MAX_DISPATCH_CALLS_PER_PLANNER_TURN:
                return False, self._payload(
                    tool_name=tool_name,
                    reason="planner_dispatch_call_limit_reached",
                    limit_name="dispatch_calls",
                    limit=MAX_DISPATCH_CALLS_PER_PLANNER_TURN,
                    current=self.round_dispatch_calls,
                )

        if self.mode == "planner" and tool_name in RESEARCH_TOOLS:
            if self.round_research_calls + 1 > MAX_RESEARCH_CALLS_PER_PLANNER_TURN:
                return False, self._payload(
                    tool_name=tool_name,
                    reason="planner_research_call_limit_reached",
                    limit_name="research_calls",
                    limit=MAX_RESEARCH_CALLS_PER_PLANNER_TURN,
                    current=self.round_research_calls,
                )

        return True, {}

    def record(self, tool_name: str) -> None:
        """Record one accepted tool call."""
        self.total_calls += 1
        if tool_name in TERMINAL_TOOLS:
            self.terminal_calls += 1
        if tool_name in WRITE_TOOLS:
            self.write_calls += 1
        if tool_name in DISPATCH_TOOLS:
            self.dispatch_calls += 1
            self.round_dispatch_calls += 1
        if tool_name in RESEARCH_TOOLS:
            self.research_calls += 1
            self.round_research_calls += 1

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
    ) -> dict[str, Any]:
        message = (
            "Worker tool call limit reached for this pass. Do not call more tools. "
            "Summarize completed work, modified files, validation status, blockers, "
            "and remaining work."
            if phase_boundary
            else f"Tool limit reached: {limit_name} is capped at {limit}."
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
            "dispatch_calls": self.dispatch_calls,
            "research_calls": self.research_calls,
            "round_dispatch_calls": self.round_dispatch_calls,
            "round_research_calls": self.round_research_calls,
        }


def limit_reached_payload(info: dict[str, Any]) -> str:
    """Serialize a rejected tool-call payload."""
    return json.dumps(info, ensure_ascii=False)


__all__ = [
    "DISPATCH_TOOLS",
    "MAX_DISPATCH_CALLS_PER_PLANNER_TURN",
    "MAX_RESEARCH_CALLS_PER_PLANNER_TURN",
    "MAX_TERMINAL_CALLS_PER_WORKER_PASS",
    "MAX_TOOL_CALLS_BY_MODE",
    "MAX_WORKER_REDISPATCHES_PER_USER_TURN",
    "MAX_WRITE_CALLS_PER_WORKER_PASS",
    "RESEARCH_TOOLS",
    "RegistryMode",
    "TERMINAL_TOOLS",
    "ToolLimitState",
    "WRITE_TOOLS",
    "limit_reached_payload",
]
