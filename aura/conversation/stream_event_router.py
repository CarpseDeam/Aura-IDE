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
from aura.conversation.single_content_gate import SingleContentGate


@dataclass
class StreamEventResult:
    full_message: dict[str, Any] | None = None
    api_error: str | None = None


class StreamEventRouter:
    """Route inbound stream events through the production content gate.

    Production SINGLE holds a round's prose until ``Done`` decides whether it
    survives: a round that ends in tool calls never projects or stores its
    pre-tool essay; any other round replays the buffer verbatim.  A round that
    dies on ``ApiError`` flushes what it generated so the user still sees it.
    """

    def __init__(
        self,
        *,
        on_event: Callable[[Event], None],
        content_gate: SingleContentGate | None = None,
    ) -> None:
        self._on_event = on_event
        self._content_gate = content_gate

    def process(self, ev: Event) -> StreamEventResult:
        # SINGLE production pre-tool content gate. Prose is held for the whole
        # round; a round that ends in tool calls never projects or stores its
        # pre-tool essay, any other round replays the buffer verbatim, and an
        # ApiError flushes what it generated (that path stores no assistant
        # message anyway).
        if self._content_gate is not None:
            if isinstance(ev, ContentDelta):
                self._content_gate.capture(ev)
                return StreamEventResult()
            if isinstance(ev, Done):
                ev = self._content_gate.resolve_done(ev, self._on_event)
            elif isinstance(ev, ApiError):
                self._content_gate.flush(self._on_event)

        self._on_event(ev)

        if isinstance(ev, Done):
            return StreamEventResult(full_message=ev.full_message)

        if isinstance(ev, ApiError):
            return StreamEventResult(api_error=ev.message)

        return StreamEventResult()


__all__ = ["StreamEventRouter", "StreamEventResult"]
