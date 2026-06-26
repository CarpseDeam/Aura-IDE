"""Worker final quarantine: buffer ContentDelta and Done events so the user
never sees a premature "done" message while final validation gates are pending.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from aura.client import ContentDelta, Done, Event


@dataclass
class WorkerStreamBuffer:
    """Buffers ContentDelta and Done events during the worker final quarantine
    phase. Non-content events (reasoning, tool calls, results, etc.) are always
    forwarded immediately.
    """

    content_events: list[ContentDelta] = field(default_factory=list)
    done_event: Done | None = None
    buffering: bool = False

    def begin_round(self) -> None:
        """Start holding worker content for one model round."""
        self.content_events.clear()
        self.done_event = None
        self.buffering = True

    def capture_or_forward(self, event: Event, on_event: Callable[[Event], None]) -> None:
        """If buffering, store ContentDelta/Done events. All other events are
        forwarded immediately via *on_event*.
        """
        if isinstance(event, ContentDelta):
            if self.buffering:
                self.content_events.append(event)
            else:
                on_event(event)
        elif isinstance(event, Done):
            if self.buffering:
                self.done_event = event
            else:
                on_event(event)
        else:
            # All other events (ReasoningDelta, ToolCallStart, ToolCallArgsDelta,
            # ToolCallEnd, ToolResult, ApiError, TerminalOutput,
            # WorkerDispatchRequested, Usage) are forwarded immediately.
            on_event(event)

    def flush(self, on_event: Callable[[Event], None]) -> None:
        """Emit all buffered content events and then the done event, if present.
        Clears the buffer after flushing.
        """
        if self.done_event is not None:
            for ev in self.content_events:
                on_event(ev)
            on_event(self.done_event)
        self.content_events.clear()
        self.done_event = None
        self.buffering = False

    def discard(self) -> None:
        """Drop all buffered events without forwarding. Resets for next round."""
        self.content_events.clear()
        self.done_event = None
        self.buffering = False

    def clear(self) -> None:
        """Reset the buffer to its initial state (alias for discard)."""
        self.discard()


__all__ = [
    "WorkerStreamBuffer",
]
