"""Read Only collaborative-turn event routing.

Owns how a frozen Read Only turn's model/tool events become canonical chat
signals plus the single authoritative usage accounting signal. A Read Only
turn is conversation-first: assistant reasoning, prose, and read/search/
research tool calls travel the canonical chat signals, nothing is projected
into the workspace, and provider Usage still reaches conversation telemetry
exactly once through the bridge's single accounting path.

This collaborator never touches the production session, the tool registry, or
the model loop. It only turns an already-emitted ``Event`` into the chat and
usage signal surface of the owning runner.
"""
from __future__ import annotations

from typing import Any, Protocol

from aura.client import (
    ApiError,
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
)


class ReadOnlySignalEmitter(Protocol):
    """The Qt signal surface a Read Only turn routes through."""

    reasoningDelta: Any
    contentDelta: Any
    toolCallStart: Any
    toolCallArgs: Any
    toolCallEnd: Any
    toolResultEmitted: Any
    usage: Any
    streamDone: Any
    apiError: Any


def emit_read_only_facts(emitter: ReadOnlySignalEmitter, ev: Any, model_id: str) -> None:
    """Route one event to the canonical chat/usage signals for a Read Only turn.

    ``emitter`` is the owning runner's Qt signal surface; ``model_id`` names
    the active model so the usage accounting signal carries the right model.
    """
    if isinstance(ev, ReasoningDelta):
        emitter.reasoningDelta.emit(ev.text)
    elif isinstance(ev, ContentDelta):
        emitter.contentDelta.emit(ev.text)
    elif isinstance(ev, ToolCallStart):
        emitter.toolCallStart.emit(ev.index, ev.id, ev.name)
    elif isinstance(ev, ToolCallArgsDelta):
        emitter.toolCallArgs.emit(ev.index, ev.args_chunk)
    elif isinstance(ev, ToolCallEnd):
        emitter.toolCallEnd.emit(ev.index)
    elif isinstance(ev, ToolResult):
        emitter.toolResultEmitted.emit(
            ev.tool_call_id, ev.name, ev.ok, ev.result, ev.extras or {}
        )
    elif isinstance(ev, Usage):
        emitter.usage.emit(
            "", model_id, ev.prompt_tokens, ev.completion_tokens,
            ev.cache_hit_tokens, ev.cache_miss_tokens,
        )
    elif isinstance(ev, Done):
        if ev.full_message:
            emitter.streamDone.emit(ev.finish_reason or "", ev.full_message)
    elif isinstance(ev, ApiError):
        from aura.config import redact_secrets
        emitter.apiError.emit(
            ev.status_code if ev.status_code is not None else -1,
            redact_secrets(ev.message),
        )


__all__ = ["emit_read_only_facts"]
