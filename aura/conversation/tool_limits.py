"""Emergency tool-call guardrails for conversation passes.

Normal control flow is handled by loop detection and planner recovery. This
module only keeps a high runaway guard so a broken model/tool loop cannot run
forever.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

RegistryMode = Literal["single", "planner", "worker", "researcher"]

WRITE_TOOLS = {
    "write_file",
    "delete_file",
    "patch_file",
}
TERMINAL_TOOLS = {"run_terminal_command", "run_and_watch"}
DISPATCH_TOOLS = {"dispatch_to_worker"}
RESEARCH_TOOLS = {"run_research"}
PLANNER_CONTEXT_TOOLS = {
    "read_file",
    "read_files",
    "list_directory",
    "glob",
    "grep_search",
    "find_usages",
    "search_codebase",
}

# High emergency brakes, not workflow budgets. These should be far above normal
# use; repeated non-progress is handled by aura.conversation.loop_detection.
MAX_TOOL_CALLS_BY_MODE: dict[RegistryMode, int] = {
    "planner": 300,
    "worker": 300,
    "single": 300,
    "researcher": 80,
}

MAX_WORKER_REDISPATCHES_PER_USER_TURN = 2

# Backward-compatible aliases for older imports. Category-specific hard caps are
# intentionally disabled; ToolLimitState does not enforce these values.
MAX_CONTEXT_CALLS_PER_PLANNER_TURN: int | None = None
MAX_TERMINAL_CALLS_PER_WORKER_PASS: int | None = None
MAX_WRITE_CALLS_PER_WORKER_PASS: int | None = None
MAX_DISPATCH_CALLS_PER_PLANNER_TURN: int | None = None
MAX_RESEARCH_CALLS_PER_PLANNER_TURN: int | None = None


@dataclass
class ToolLimitState:
    """Tracks tool-call counts and enforces only high emergency totals."""

    mode: RegistryMode
    total_calls: int = 0
    terminal_calls: int = 0
    write_calls: int = 0
    dispatch_calls: int = 0
    research_calls: int = 0
    planner_context_calls: int = 0
    round_dispatch_calls: int = 0
    round_research_calls: int = 0

    def begin_model_round(self) -> None:
        """Reset per-round telemetry counters."""
        self.round_dispatch_calls = 0
        self.round_research_calls = 0

    def check(self, tool_name: str) -> tuple[bool, dict[str, Any]]:
        """Return whether *tool_name* may run plus a JSON-ready reason payload."""
        max_total = MAX_TOOL_CALLS_BY_MODE.get(self.mode, MAX_TOOL_CALLS_BY_MODE["single"])
        if self.total_calls + 1 > max_total:
            phase_boundary = self.mode == "worker"
            return False, self._payload(
                tool_name=tool_name,
                reason=f"{self.mode}_emergency_tool_call_limit_reached",
                limit_name="total_calls",
                limit=max_total,
                current=self.total_calls,
                recoverable=phase_boundary,
                phase_boundary=phase_boundary,
            )
        return True, {}

    def record(self, tool_name: str) -> None:
        """Record one accepted tool call for telemetry."""
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
        if self.mode == "planner" and tool_name in PLANNER_CONTEXT_TOOLS:
            self.planner_context_calls += 1

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
            "Emergency tool-call guard reached for this worker pass. Do not call more "
            "tools. Summarize completed work, modified files, validation status, "
            "blockers, and remaining work so the planner can adjust."
            if phase_boundary
            else (
                "Emergency tool-call guard reached. Stop calling tools and report the "
                "current state or ask one concise clarifying question."
            )
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
            "planner_context_calls": self.planner_context_calls,
            "round_dispatch_calls": self.round_dispatch_calls,
            "round_research_calls": self.round_research_calls,
        }


def limit_reached_payload(info: dict[str, Any]) -> str:
    """Serialize a rejected tool-call payload."""
    return json.dumps(info, ensure_ascii=False)


__all__ = [
    "DISPATCH_TOOLS",
    "MAX_CONTEXT_CALLS_PER_PLANNER_TURN",
    "MAX_DISPATCH_CALLS_PER_PLANNER_TURN",
    "MAX_RESEARCH_CALLS_PER_PLANNER_TURN",
    "MAX_TERMINAL_CALLS_PER_WORKER_PASS",
    "MAX_TOOL_CALLS_BY_MODE",
    "MAX_WORKER_REDISPATCHES_PER_USER_TURN",
    "MAX_WRITE_CALLS_PER_WORKER_PASS",
    "RESEARCH_TOOLS",
    "PLANNER_CONTEXT_TOOLS",
    "RegistryMode",
    "TERMINAL_TOOLS",
    "ToolLimitState",
    "WRITE_TOOLS",
    "limit_reached_payload",
]
