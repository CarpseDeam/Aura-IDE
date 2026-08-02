"""Stream-event visibility and routing rules for ConversationManager."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aura.client import (
    ApiError,
    ContentDelta,
    Done,
    Event,
)
from aura.conversation.planner_stream_hygiene import PlannerStreamHygiene
from aura.conversation.single_content_gate import SingleContentGate
from aura.conversation.worker_stream_buffer import WorkerStreamBuffer


@dataclass
class StreamEventResult:
    full_message: dict[str, Any] | None = None
    api_error: str | None = None


class StreamEventRouter:
    """Route inbound stream events through hygiene filtering and worker-buffer
    capture before forwarding to the UI.
    """

    def __init__(
        self,
        *,
        planner_hygiene: PlannerStreamHygiene | None,
        on_event: Callable[[Event], None],
        mode: str = "single",
        stream_buffer: WorkerStreamBuffer | None = None,
        content_gate: SingleContentGate | None = None,
        discard_prose_final: bool = False,
    ) -> None:
        self._planner_hygiene = planner_hygiene
        self._on_event = on_event
        self._mode = mode
        self._stream_buffer = stream_buffer
        self._content_gate = content_gate
        self._discard_prose_final = discard_prose_final

    def process(self, ev: Event) -> StreamEventResult:
        # 1. Planner ContentDelta filter
        if self._planner_hygiene is not None and isinstance(ev, ContentDelta):
            filtered_text = self._planner_hygiene.filter_delta(ev.text)
            if not filtered_text:
                return StreamEventResult()
            ev = ContentDelta(text=filtered_text)

        # 2. Planner Done flush/sanitize (elif)
        elif self._planner_hygiene is not None and isinstance(ev, Done):
            flush_text = self._planner_hygiene.flush()
            if flush_text:
                self._on_event(ContentDelta(text=flush_text))
            if isinstance(ev.full_message, dict):
                content = ev.full_message.get("content")
                if isinstance(content, str):
                    ev.full_message["content"] = self._planner_hygiene.sanitize_message_text(content)

        # 3. SINGLE production pre-tool content gate.
        #
        # Prose is held for the whole round.  A round that ends in tool calls
        # never projects or stores its pre-tool essay; any other round replays
        # the buffer verbatim so the final answer stays chat-owned.  A round
        # that dies on ApiError flushes what it generated — the user should
        # still see it, and that path stores no assistant message anyway.
        if self._content_gate is not None:
            if isinstance(ev, ContentDelta):
                self._content_gate.capture(ev)
                return StreamEventResult()
            if isinstance(ev, Done):
                ev = self._content_gate.resolve_done(
                    ev, self._on_event, discard_prose_final=self._discard_prose_final
                )
            elif isinstance(ev, ApiError):
                self._content_gate.flush(self._on_event)

        # 4. Worker stream_buffer routing / normal forwarding
        if self._mode == "worker" and self._stream_buffer is not None:
            self._stream_buffer.capture_or_forward(ev, self._on_event)
        else:
            self._on_event(ev)

        # 5. Done full_message capture
        if isinstance(ev, Done):
            return StreamEventResult(full_message=ev.full_message)

        # 6. ApiError detection
        if isinstance(ev, ApiError):
            return StreamEventResult(api_error=ev.message)

        # 7. Default
        return StreamEventResult()


__all__ = ["StreamEventRouter", "StreamEventResult"]
