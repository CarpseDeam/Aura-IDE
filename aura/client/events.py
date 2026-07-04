"""Streaming event types yielded by DeepSeekClient and ConversationManager.

These are intentionally simple dataclasses so they cross thread boundaries
cleanly via Qt signals. Never raise — the client yields ApiError instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class ContentDelta:
    text: str


@dataclass
class ToolCallStart:
    index: int
    id: str
    name: str


@dataclass
class ToolCallArgsDelta:
    index: int
    args_chunk: str


@dataclass
class ToolCallEnd:
    index: int


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int


@dataclass
class Done:
    finish_reason: str | None
    full_message: dict[str, Any]
    """Complete assistant message ready to append to history.

    Always contains keys: role, content (str | None), reasoning_content (str | None).
    Contains tool_calls (list) only when finish_reason == "tool_calls".
    """


@dataclass
class ApiError:
    status_code: int | None
    message: str


@dataclass
class ToolResult:
    """Emitted by the manager (not the client) after each tool runs."""
    tool_call_id: str
    name: str
    ok: bool
    result: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerDispatchRequested:
    """Emitted by the planner manager before it blocks waiting for a worker
    dispatch. Signals the GUI to render a SpecCard with Dispatch/Edit/Cancel
    controls. The dispatch callback (registered via send()) is what actually
    blocks until the user decides and the worker completes.
    """
    tool_call_id: str
    goal: str
    files: list[str]
    spec: str
    acceptance: str
    summary: str


@dataclass
class TerminalOutput:
    tool_call_id: str
    text: str  # chunk of stdout/stderr output


@dataclass
class AgentProcessStarted:
    process_id: str
    label: str
    command: str


@dataclass
class AgentProcessOutput:
    process_id: str
    text: str


@dataclass
class AgentProcessFinished:
    process_id: str
    exit_code: int


Event = (
    ReasoningDelta
    | ContentDelta
    | ToolCallStart
    | ToolCallArgsDelta
    | ToolCallEnd
    | Usage
    | Done
    | ApiError
    | ToolResult
    | WorkerDispatchRequested
    | TerminalOutput
    | AgentProcessStarted
    | AgentProcessOutput
    | AgentProcessFinished
)
