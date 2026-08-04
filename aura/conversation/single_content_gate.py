"""Pre-tool content gating for the SINGLE production runtime.

The production model streams ordinary prose on *every* round, including rounds
that end in tool calls.  That prose is a pre-tool essay: the model narrating the
plan it is about to execute.  Chat should not show it — the tool cards that
follow are the visible record of the round — so this gate withholds it from the
live projection.

This gate is **presentation-only**.  It decides what the user sees, never what
the model is sent.  Blanking the completed assistant message's ``content`` (the
gate's earlier behavior) destroyed the model's own working state: the next round
received the tool call and its result but not the plan that produced them, so
the model had to reconstruct that plan from scratch on every round.  The
canonical assistant message is therefore left exactly as the provider produced
it.

Contract — SINGLE production rounds only:

* ``ContentDelta`` is buffered for the whole round; nothing reaches chat while
  the round is still undecided.
* On ``Done`` **with** ``tool_calls`` the buffer is dropped, so the pre-tool
  essay is never projected into chat.  ``Done.full_message`` is forwarded
  untouched — ``content``, ``reasoning_content``, and ``tool_calls`` all exactly
  as produced — so canonical history and the next request keep the round's
  working state.  The GUI does not render ``full_message['content']`` on a
  tool-calling round (``MainWindow._on_stream_done`` only finalises markdown),
  so nothing is projected twice.
* On ``Done`` **without** ``tool_calls`` the buffered deltas are replayed in
  order and the message is forwarded unchanged: the final answer is normal
  chat-owned prose and persists normally.
* If the round ends without a ``Done`` — cancellation or an API error — the
  buffer is flushed so the user still sees what was generated.  Nothing extra
  is stored: that path already appends no assistant message.

Reasoning, tool cards, TODOs, diffs, terminal output, validation, and activity
never pass through this gate; they keep their existing owners.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from aura.client import ContentDelta, Done, Event

EventCallback = Callable[[Event], None]


@dataclass
class SingleContentGate:
    """Hold one round's ContentDelta until ``Done`` says who owns it."""

    _buffered: list[ContentDelta] = field(default_factory=list)

    #: Telemetry — how many rounds and characters of pre-tool essay were held
    #: back from the visible projection.  The message itself keeps them.
    suppressed_rounds: int = 0
    suppressed_chars: int = 0

    # ---- round lifecycle -------------------------------------------------

    def begin_round(self) -> None:
        """Start holding content for one model round."""
        self._buffered.clear()

    def capture(self, event: ContentDelta) -> None:
        """Hold one content delta instead of forwarding it."""
        self._buffered.append(event)

    def resolve_done(self, event: Done, on_event: EventCallback) -> Done:
        """Decide the round and return the ``Done`` that should be forwarded.

        A tool-calling round drops its buffer, so chat never shows the pre-tool
        essay, and returns the ``Done`` **unmodified**: the completed assistant
        message is the model's working state for the next round and belongs in
        canonical history exactly as produced.  Any other round replays its
        buffer verbatim.
        """
        message = event.full_message if isinstance(event.full_message, dict) else None
        if message is not None and message.get("tool_calls"):
            self.suppressed_rounds += 1
            self.suppressed_chars += sum(
                len(str(delta.text or "")) for delta in self._buffered
            )
            self._buffered.clear()
            return event

        self.flush(on_event)
        return event

    def flush(self, on_event: EventCallback) -> None:
        """Emit whatever is still held, in order. No-op once a round resolved."""
        if not self._buffered:
            return
        buffered = list(self._buffered)
        self._buffered.clear()
        for delta in buffered:
            on_event(delta)

    # ---- introspection ---------------------------------------------------

    @property
    def buffered_text(self) -> str:
        return "".join(str(delta.text or "") for delta in self._buffered)


__all__ = ["SingleContentGate"]
