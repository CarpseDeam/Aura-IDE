"""The focused round's ``Done`` is held until the manager has validated it.

``StreamEventRouter`` forwards events as they arrive, which is right for every
ordinary round: the provider's ``Done`` *is* the round's terminal event. A
focused action request is the exception. Whether its response honoured the
exactly-one-tool-call contract is not knowable until the full message has been
inspected, and a violating response is discarded whole — so it must not first
hand the UI a ``Done`` of its own and then receive the factual
provider-contract-failure ``Done`` as well.

Asserted here, at the router itself:

* deferral is opt-in — every other mode streams and forwards exactly as before;
* a deferred ``Done`` reaches nobody until it is released;
* releasing forwards the *original* event, exactly once, however often release
  is called;
* discarding never projects it;
* ContentDelta, ApiError, planner filtering, and worker buffering are untouched
  by deferral.
"""

from __future__ import annotations

from typing import Any

from aura.client.events import ApiError, ContentDelta, Done
from aura.conversation.single_content_gate import SingleContentGate
from aura.conversation.stream_event_router import StreamEventRouter


def _done(**message: Any) -> Done:
    return Done(finish_reason="stop", full_message=dict(message))


def _router(events: list[Any], **kwargs: Any) -> StreamEventRouter:
    return StreamEventRouter(
        planner_hygiene=None,
        on_event=events.append,
        **kwargs,
    )


# ── ordinary rounds are untouched ───────────────────────────────────────────


def test_without_deferral_done_is_forwarded_as_it_arrives() -> None:
    events: list[Any] = []
    router = _router(events)
    done = _done(role="assistant", content="answer")

    result = router.process(done)

    assert events == [done]
    assert result.full_message == done.full_message
    assert router.has_deferred_done is False
    assert router.release_deferred_done() is False


def test_deferral_does_not_hold_back_deltas_or_api_errors() -> None:
    events: list[Any] = []
    router = _router(events, defer_done=True)
    delta = ContentDelta(text="thinking out loud")
    error = ApiError(status_code=500, message="upstream exploded")

    router.process(delta)
    result = router.process(error)

    assert events == [delta, error]
    assert result.api_error == "upstream exploded"


# ── deferral ────────────────────────────────────────────────────────────────


def test_a_deferred_done_reaches_nobody_until_released() -> None:
    events: list[Any] = []
    router = _router(events, defer_done=True)
    done = _done(role="assistant", tool_calls=[{"id": "w1"}])

    result = router.process(done)

    assert events == [], "the held Done must not reach the UI"
    assert router.has_deferred_done is True
    # The manager still gets the message it has to validate.
    assert result.full_message == done.full_message


def test_release_forwards_the_original_event_exactly_once() -> None:
    events: list[Any] = []
    router = _router(events, defer_done=True)
    done = _done(role="assistant", tool_calls=[{"id": "w1"}])
    router.process(done)

    assert router.release_deferred_done() is True
    assert events == [done], "the original event is what gets forwarded"
    assert events[0] is done

    assert router.release_deferred_done() is False
    assert router.release_deferred_done() is False
    assert events == [done], "release is exactly-once, not once-per-call"
    assert router.has_deferred_done is False


def test_discard_never_projects_the_held_done() -> None:
    events: list[Any] = []
    router = _router(events, defer_done=True)
    router.process(_done(role="assistant", content="prose instead of a call"))

    router.discard_deferred_done()

    assert events == []
    assert router.has_deferred_done is False
    assert router.release_deferred_done() is False, (
        "a discarded Done can never be recovered and projected later"
    )


def test_the_content_gate_still_resolves_a_deferred_round() -> None:
    """Deferral changes *when* the Done is forwarded, not how the round's
    prose is decided: a tool-calling round still drops its pre-tool essay."""
    events: list[Any] = []
    gate = SingleContentGate()
    gate.begin_round()
    router = _router(events, content_gate=gate, defer_done=True)

    router.process(ContentDelta(text="First I will open the loader..."))
    assert events == [], "pre-tool prose is held by the gate"

    done = _done(role="assistant", content="essay", tool_calls=[{"id": "w1"}])
    router.process(done)
    assert events == []

    router.release_deferred_done()
    assert events == [done]
    assert done.full_message["content"] == "", "the pre-tool essay was stored"
    assert gate.buffered_text == ""


def test_worker_buffering_is_unaffected_when_deferral_is_off() -> None:
    class _Buffer:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        def capture_or_forward(self, ev: Any, on_event: Any) -> None:
            self.seen.append(ev)

    events: list[Any] = []
    buffer = _Buffer()
    router = _router(events, mode="worker", stream_buffer=buffer)
    done = _done(role="assistant", content="worker answer")

    router.process(done)

    assert buffer.seen == [done]
    assert events == []
