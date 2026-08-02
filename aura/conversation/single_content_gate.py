"""Pre-tool content gating for the SINGLE production runtime.

The production model streams ordinary prose on *every* round, including rounds
that end in tool calls.  That prose is a pre-tool essay: the model narrating the
plan it is about to execute.  Streaming it straight to chat and then storing it
verbatim in history makes the model read its own narration back on the next
round and restate it — the circular-planning failure this gate removes.  Prompt
wording cannot fix it, because the loop is in the transport, not the prompt.

Contract — SINGLE production rounds only:

* ``ContentDelta`` is buffered for the whole round; nothing reaches chat while
  the round is still undecided.
* On ``Done`` **with** ``tool_calls`` the buffer is dropped and the assistant
  message's ``content`` is normalised to ``""`` before the message is projected
  or stored.  ``tool_calls`` are left byte-for-byte intact and
  ``reasoning_content`` is left exactly as the provider produced it, so every
  backend (DeepSeek, OpenRouter, Anthropic, OpenAI-style) still receives a
  valid assistant tool-call message.
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
from typing import Any, Callable

from aura.client import ContentDelta, Done, Event

EventCallback = Callable[[Event], None]


def normalize_tool_call_message(message: dict[str, Any]) -> dict[str, Any]:
    """Strip pre-tool prose from an assistant tool-call message, in place.

    ``content`` becomes ``""`` — the minimal value every supported backend
    accepts alongside ``tool_calls``.  ``tool_calls`` and ``reasoning_content``
    are untouched: DeepSeek thinking mode requires ``reasoning_content`` to be
    replayed, and the Anthropic adapter drops empty text blocks on its own.
    """
    message["content"] = ""
    return message


@dataclass
class SingleContentGate:
    """Hold one round's ContentDelta until ``Done`` says who owns it."""

    _buffered: list[ContentDelta] = field(default_factory=list)

    #: Whether the current round is a focused action round whose prose final is
    #: held for the manager to discard (a provider-contract violation).
    _holding_final: bool = False

    #: Telemetry — how many rounds and characters of pre-tool essay were dropped.
    suppressed_rounds: int = 0
    suppressed_chars: int = 0

    # ---- round lifecycle -------------------------------------------------

    def begin_round(self) -> None:
        """Start holding content for one model round."""
        self._buffered.clear()
        self._holding_final = False

    def capture(self, event: ContentDelta) -> None:
        """Hold one content delta instead of forwarding it."""
        self._buffered.append(event)

    def resolve_done(self, event: Done, on_event: EventCallback, *, discard_prose_final: bool = False) -> Done:
        """Decide the round and return the ``Done`` that should be forwarded.

        A tool-calling round drops its buffer and yields a message whose
        ``content`` is normalised, so neither chat nor history ever sees the
        pre-tool essay.  Any other round replays its buffer verbatim.

        ``discard_prose_final`` marks a focused action round: its contract is
        exactly one tool call, so a prose ``Done`` is a violation and the
        buffer is held instead of replayed — the manager discards it and
        streams the single factual failure response.
        """
        message = event.full_message if isinstance(event.full_message, dict) else None
        if message is not None and message.get("tool_calls"):
            self.suppressed_rounds += 1
            self.suppressed_chars += sum(
                len(str(delta.text or "")) for delta in self._buffered
            )
            self.suppressed_chars += len(str(message.get("content") or ""))
            self._buffered.clear()
            normalize_tool_call_message(message)
            return event

        if discard_prose_final:
            self._holding_final = True
            return event

        self.flush(on_event)
        return event

    def flush(self, on_event: EventCallback) -> None:
        """Emit whatever is still held, in order. No-op once a round resolved.

        A focused action round's prose final is never replayed here — it is
        held for the manager, which either discards it (contract violation) or
        lets the completion path own the turn's one factual response.
        """
        if self._holding_final:
            return
        if not self._buffered:
            return
        buffered = list(self._buffered)
        self._buffered.clear()
        for delta in buffered:
            on_event(delta)

    def discard_buffer(self) -> None:
        """Drop whatever is still held without emitting it.

        Used when a focused action round ends in a provider-contract failure:
        the violating pre-tool prose must never reach chat or history, so the
        buffered deltas are thrown away rather than flushed.
        """
        self._buffered.clear()
        self._holding_final = False

    # ---- introspection ---------------------------------------------------

    @property
    def buffered_text(self) -> str:
        return "".join(str(delta.text or "") for delta in self._buffered)


__all__ = ["SingleContentGate", "normalize_tool_call_message"]
