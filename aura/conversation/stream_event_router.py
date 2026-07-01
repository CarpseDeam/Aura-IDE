"""Stream-event visibility and routing rules for ConversationManager."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aura.client import ApiError, ContentDelta, Done, Event, ReasoningDelta
from aura.conversation.planner_stream_hygiene import PlannerStreamHygiene
from aura.conversation.worker_stream_buffer import WorkerStreamBuffer


@dataclass
class StreamEventResult:
    full_message: dict[str, Any] | None = None
    api_error: str | None = None


class StreamEventRouter:
    """Route inbound stream events through hygiene filtering, silent-preflight
    suppression, and worker-buffer capture before forwarding to the UI.
    """

    def __init__(
        self,
        *,
        planner_hygiene: PlannerStreamHygiene | None,
        on_event: Callable[[Event], None],
        mode: str = "single",
        stream_buffer: WorkerStreamBuffer | None = None,
    ) -> None:
        self._planner_hygiene = planner_hygiene
        self._on_event = on_event
        self._mode = mode
        self._stream_buffer = stream_buffer

    def process(self, ev: Event, *, silent_preflight: bool = False) -> StreamEventResult:
        # 1. Planner ContentDelta filter
        if self._planner_hygiene is not None and isinstance(ev, ContentDelta):
            filtered_text = self._planner_hygiene.filter_delta(ev.text)
            if not filtered_text:
                return StreamEventResult()
            ev = ContentDelta(text=filtered_text)

        # 2. Planner Done flush/sanitize (elif)
        elif self._planner_hygiene is not None and isinstance(ev, Done):
            if not silent_preflight:
                flush_text = self._planner_hygiene.flush()
                if flush_text:
                    self._on_event(ContentDelta(text=flush_text))
            if isinstance(ev.full_message, dict):
                content = ev.full_message.get("content")
                if isinstance(content, str):
                    ev.full_message["content"] = self._planner_hygiene.sanitize_message_text(content)

        # 3. Silent preflight suppression
        if silent_preflight:
            if isinstance(ev, (ContentDelta, ReasoningDelta)):
                return StreamEventResult()
            if isinstance(ev, Done):
                return StreamEventResult(full_message=ev.full_message)

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
