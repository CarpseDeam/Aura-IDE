"""The model -> tool -> model loop, with nothing Qt and nothing global in it.

This is Aura's one coding-agent loop, extracted so more than one agent can run
it.  It is deliberately low level: it owns a round, not a turn.

What it owns
------------
One agent's rounds against one injected backend stream:

1. Send the caller's canonical history snapshot with the caller's frozen tool
   catalog, model, and thinking mode.
2. Forward every provider event to ``on_event`` as it arrives.
3. Append the complete assistant response exactly as received.
4. If it carries tool calls, hand the whole batch to the tool round and call
   the same model again; otherwise the loop is finished.
5. Cancellation or provider failure stops the loop without claiming success,
   and cancellation repairs the interrupted turn's tool-call pairing.

What it does not own
--------------------
Everything that makes a *turn* a turn.  It does not build or freeze skill turn
state, resolve a tool catalog, decide a system prompt, choose a provider, or
touch a stream registry.  It never reaches for a process-global hook: the
backend arrives as an injected ``stream`` callable, so a second agent can run
this same loop against ``APIAgentBackend(provider=...).stream`` for an entirely
different provider without disturbing the root production stream.  There is no
Qt here, and no import of it.

For the root production agent, :class:`~aura.conversation.manager.
ConversationManager` remains the sole owner of the canonical History, the
Skills turn state, and the user-facing turn; it composes those and then drives
its rounds through this loop.

There is no router, no classifier, no counter, no budget, and no continuation
injected on the model's behalf.  Nothing but the model, cancellation, or a
provider failure ends the loop.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from aura.client import ApiError, Done, Event
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tools._types import ApprovalCallback
from aura.conversation.validation_orchestrator import ValidationCommandSpec

_log = logging.getLogger(__name__)

EventCallback = Callable[[Event], None]

#: The injected backend stream.  Called once per round with ``messages``,
#: ``tools``, ``model``, ``thinking``, ``cancel_event``, and ``temperature``
#: keyword arguments — exactly the shape of ``APIAgentBackend.stream`` — and
#: expected to yield :class:`~aura.client.events.Event` objects.
StreamCallable = Callable[..., Iterable[Event]]

#: Structured payload paired to a tool call that was cancelled before the agent
#: received its authoritative result. Deliberately fail-closed: never
#: ``applied``, never successful, and it explicitly refuses to claim the tool
#: made no workspace changes — the operation's effects are simply unknown.
_CANCELLATION_TOOL_RESULT_TEMPLATE: dict[str, Any] = {
    "ok": False,
    "cancelled": True,
    "recoverable": False,
    "failure_class": "cancelled",
    "execution_status": "interrupted_before_authoritative_result",
    "message": (
        "Cancelled before Aura received an authoritative result. Do not "
        "infer that the operation completed or that a mutation applied."
    ),
}


def _synthetic_cancellation_result(tool_name: str) -> str:
    """Return the JSON tool-result payload for one cancelled tool call.

    The structured payload exists only to restore the transcript's tool-call
    pairing.  It must never be read as a successful execution, a validation
    pass, or an applied mutation: ``ok`` is false, ``applied`` is absent, and
    the message states that nothing about the operation's effect may be
    inferred.
    """
    payload = dict(_CANCELLATION_TOOL_RESULT_TEMPLATE)
    payload["tool"] = tool_name or "<unknown>"
    return json.dumps(payload, ensure_ascii=False)


def _assistant_tool_call_name(call: Any) -> str:
    """Return the tool name for one assistant tool-call entry, or ``""``.

    Repair/reporting only: a malformed entry yields the empty string so the
    caller can decide whether safe pairing is even possible.
    """
    if not isinstance(call, dict):
        return ""
    function = call.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


def _repairable_tool_call_block(tool_calls: Any) -> bool:
    """Return whether every entry in a tool-call list can receive a paired result.

    A block is repairable when each entry is a dict carrying a usable ``id`` —
    the only key a synthetic tool result needs to pair back.  A non-dict entry,
    a missing id, or an empty id means no synthetic result could safely pair,
    so the newest block is removed instead of fabricating unpaired results.
    """
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, dict):
            return False
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            return False
    return True


class LoopStop(str, Enum):
    """Why the loop stopped. A fact about the run, never a verdict on it."""

    #: The model answered without calling tools.
    COMPLETED = "completed"
    #: The user stopped the run.
    CANCELLED = "cancelled"
    #: The provider failed; the turn stops without claiming success.
    API_ERROR = "api_error"
    #: The stream ended without a ``Done``, so there was no response to append.
    NO_RESPONSE = "no_response"


@dataclass(frozen=True)
class AgentLoopOutcome:
    """How one loop run ended."""

    stop: LoopStop

    @property
    def completed(self) -> bool:
        return self.stop is LoopStop.COMPLETED

    @property
    def cancelled(self) -> bool:
        return self.stop is LoopStop.CANCELLED


class AgentLoop:
    """Run one agent's model -> tool -> model rounds over an injected stream.

    Every dependency is explicit and passed in: the History the rounds append
    to, the backend ``stream`` callable, and the :class:`ToolRoundRunner` that
    executes a batch. Nothing is looked up from a registry or a global.
    """

    def __init__(
        self,
        *,
        history: History,
        stream: StreamCallable,
        tool_round: ToolRoundRunner,
        label: str = "agent_stream",
        hook_name: str = "",
    ) -> None:
        self._history = history
        self._stream = stream
        self._tool_round = tool_round
        #: Log prefix for this agent's rounds.
        self._label = label
        #: Identity of the injected stream, recorded in the round's log line.
        self._hook_name = hook_name

    @property
    def history(self) -> History:
        return self._history

    def run(
        self,
        *,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        model: str,
        thinking: str,
        tool_defs: list[dict[str, Any]],
        temperature: float = 0.7,
        skill_turn: Any = None,
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
    ) -> AgentLoopOutcome:
        """Run rounds until the model stops calling tools.

        The caller has already appended the request to history, frozen
        ``tool_defs``, and (for the root agent) frozen its skill turn state.

        ``skill_turn`` and ``explicit_validation_commands`` are opaque here:
        the loop never reads them, and forwards them verbatim to the tool
        round. Ownership of both stays with the caller.

        One shape, every round:

        1. Send canonical history with the caller's stable tool catalog, the
           caller-selected ``model``, and the caller-selected ``thinking`` mode.
        2. Forward the provider stream to ``on_event`` as it arrives.
        3. Append the complete assistant response exactly as received.
        4. If it carries tool calls, validate them structurally, execute the
           whole batch, append exactly one truthful result per call in original
           call order, and call the same model again.
        5. If it carries no tool calls, the loop is finished.
        6. Cancellation or provider failure stops the loop without claiming the
           work succeeded.

        Nothing else ends a run. There is no round, tool, token, or time
        ceiling; no injected continuation; no required-tool round; no rejection
        of a repeated read; and no classification of the request deciding
        whether the model is allowed to stop.
        """
        while True:
            if cancel_event.is_set():
                self.repair_cancelled_turn(on_event)
                return AgentLoopOutcome(LoopStop.CANCELLED)

            full_message: dict[str, Any] | None = None
            terminal_done_before_cancel = False
            api_error_cancelled_at_receive: bool | None = None

            # The one request shape: the same stable catalog, the user's model,
            # and the user's thinking mode, on every round of the turn.
            _log.info(
                "%s_start model=%s thinking=%s hook_name=%s",
                self._label, model, thinking, self._hook_name,
            )
            _first_event = True

            # Pass a deep-copied canonical history snapshot. Nothing is
            # compacted, pruned, or rewritten here, so the round's own plan and
            # reasoning remain durable for the UI and replay inspection. The
            # client/protocol layer owns the provider-specific wire projection.
            request_messages = self._history.for_api()

            for ev in self._stream(
                messages=request_messages,
                tools=tool_defs,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
                temperature=temperature,
            ):
                cancelled_when_received = cancel_event.is_set()
                if _first_event:
                    _log.info("%s_first_event model=%s", self._label, model)
                    _first_event = False

                # Every event is forwarded as produced, including prose emitted
                # before tool calls. Nothing is buffered, withheld, or blanked.
                on_event(ev)

                if isinstance(ev, Done):
                    full_message = ev.full_message
                    terminal_done_before_cancel = bool(
                        full_message is not None
                        and not (full_message.get("tool_calls") or [])
                        and not cancelled_when_received
                    )
                elif isinstance(ev, ApiError):
                    _log.info("%s_api_error model=%s", self._label, model)
                    # Settle terminal/cancellation truth below before
                    # classifying the provider error. A cancellation observed
                    # while transport was waiting wins over a later ApiError;
                    # a terminal Done fully received first remains completed.
                    api_error_cancelled_at_receive = cancelled_when_received
                    break

            _log.info("%s_done model=%s", self._label, model)

            if terminal_done_before_cancel and full_message is not None:
                # A terminal assistant Done is authoritative once fully
                # received. A cancellation observed only after that event
                # cannot retroactively turn the completed answer into a
                # cancelled partial result.
                self._history.append_assistant(full_message)
                return AgentLoopOutcome(LoopStop.COMPLETED)

            if api_error_cancelled_at_receive is False:
                # The provider error had already arrived before cancellation.
                # A callback-triggered cancellation cannot rewrite that order.
                return AgentLoopOutcome(LoopStop.API_ERROR)

            if cancel_event.is_set() or api_error_cancelled_at_receive is True:
                # Cancellation is not a verdict: whatever the stream already
                # completed keeps its terminal event.
                # If we have some content but no tool calls, we can keep it.
                # If it's empty or has orphaned tool calls, we must strip it.
                if full_message is not None:
                    # DeepSeek/OpenRouter specific: reasoning_content is NOT 'content' for the API.
                    # Standard APIs REQUIRE 'content' (string) or 'tool_calls' (list).
                    content = full_message.get("content")
                    reasoning = full_message.get("reasoning_content")

                    has_any_text = bool(content or reasoning)
                    if has_any_text:
                        full_message.pop("tool_calls", None)
                        # Normalize content to string so API doesn't reject it
                        if full_message.get("content") is None:
                            full_message["content"] = ""
                        self._history.append_assistant(full_message)
                    else:
                        self.repair_cancelled_turn(on_event)
                else:
                    self.repair_cancelled_turn(on_event)
                return AgentLoopOutcome(LoopStop.CANCELLED)

            if full_message is None:
                # The stream ended without a Done. There is no assistant
                # response to append and nothing to execute.
                return AgentLoopOutcome(LoopStop.NO_RESPONSE)

            # The complete assistant response, exactly as received.
            self._history.append_assistant(full_message)

            tool_calls = full_message.get("tool_calls") or []
            if not tool_calls:
                return AgentLoopOutcome(LoopStop.COMPLETED)

            tool_round = self._tool_round.run(
                tool_calls=tool_calls,
                skill_turn=skill_turn,
                on_event=on_event,
                approval_cb=approval_cb,
                cancel_event=cancel_event,
                cleanup_cancelled=self.repair_cancelled_turn,
                explicit_validation_commands=explicit_validation_commands,
                tool_defs=tool_defs,
            )

            if tool_round.cancelled:
                return AgentLoopOutcome(LoopStop.CANCELLED)

    def repair_cancelled_turn(self, on_event: EventCallback) -> None:
        """Repair this agent's current turn after a cancellation.

        Cancellation can interrupt an assistant tool-call block mid-batch:
        history then ends at an assistant message whose calls have no results.
        The old behaviour rewound the whole turn back to the preceding user
        message, silently erasing completed reads, applied writes, terminal
        commands, and validation evidence from the same turn even though the
        workspace was already modified.

        This repairs instead of rewinding.  Every completed assistant/tool-
        result block is preserved byte-for-byte; only the newest incomplete
        assistant tool-call block is touched, and only by appending one
        structured synthetic cancellation result for each call whose
        authoritative result never arrived, in call order, so the provider's
        tool-call pairing stays valid.  Synthetic results are fail-closed —
        never ``applied``, never successful — and exist only to restore that
        pairing.  Repair is idempotent: a second call finds every call already
        paired and changes nothing.

        A newest block too malformed to pair safely removes only that newest
        malformed assistant/result block; the turn is never rewound.
        """
        if not self._history.messages:
            on_event(ApiError(status_code=None, message="Cancelled."))
            return

        messages = self._history.messages
        user_idx = self._history.latest_real_user_index()
        start = (user_idx + 1) if user_idx is not None else 0

        for i in range(len(messages) - 1, start - 1, -1):
            msg = messages[i]
            if msg.get("role") != "assistant":
                continue

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                # Newest assistant block of the turn, without tool calls. A
                # block that carried only reasoning or prose is preserved —
                # partial-stream output survives cancellation — while an empty
                # block (no content, reasoning, or calls) is stripped exactly
                # as before.
                if not msg.get("content") and not msg.get("reasoning_content"):
                    self._history.truncate_after(i)
                break

            if not _repairable_tool_call_block(tool_calls):
                # A malformed block no synthetic result could pair to. Remove
                # only this newest malformed assistant/result block; never the
                # whole turn.
                self._history.truncate_after(i)
                break

            # Authoritative results that already arrived before the cancel are
            # kept verbatim. Collect them so an existing result is never
            # replaced by a synthetic one.
            present: set[str] = set()
            for j in range(i + 1, len(messages)):
                m = messages[j]
                if m.get("role") == "tool":
                    present.add(m.get("tool_call_id"))

            missing = [
                call for call in tool_calls if call.get("id") not in present
            ]
            if not missing:
                break  # every call is already paired; nothing to repair

            for call in tool_calls:
                if call.get("id") in present:
                    continue
                self._history.append_tool_result(
                    call["id"],
                    _synthetic_cancellation_result(
                        _assistant_tool_call_name(call)
                    ),
                )
            break

        on_event(ApiError(status_code=None, message="Cancelled."))


__all__ = [
    "AgentLoop",
    "AgentLoopOutcome",
    "LoopStop",
    "EventCallback",
    "StreamCallable",
]
