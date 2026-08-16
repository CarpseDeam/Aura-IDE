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
class TerminalOutput:
    tool_call_id: str
    text: str  # chunk of stdout/stderr output


@dataclass
class TerminalCommandStarted:
    """Authoritative start of a real terminal command submission."""

    tool_call_id: str
    command: str
    cwd: str

    @property
    def starting_cwd(self) -> str:
        """Descriptive alias for the cwd before command submission."""
        return self.cwd


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


@dataclass(frozen=True)
class FileEditChange:
    """One file's identity and content within a file-edit lifecycle event."""

    change_id: str
    path: str  # normalized, workspace-relative
    action: str  # "create" | "modify" | "delete"
    old_content: str = ""
    new_content: str = ""


@dataclass
class FileEditLifecycle:
    """One phase of one file-edit's authoritative lifecycle.

    ``phase`` is one of: "proposed", "awaiting_approval", "applied",
    "rejected", "failed". Only the write owner that actually performed the
    filesystem operation may emit "applied" -- every other phase must never
    be mistaken for proof that a change landed on disk.
    """

    tool_call_id: str
    tool_name: str
    phase: str
    changes: tuple[FileEditChange, ...] = ()
    reason: str = ""


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
    | TerminalCommandStarted
    | TerminalOutput
    | AgentProcessStarted
    | AgentProcessOutput
    | AgentProcessFinished
    | FileEditLifecycle
)
