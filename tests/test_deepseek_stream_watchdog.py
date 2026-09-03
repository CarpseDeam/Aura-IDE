"""Bounded-liveness tests for the normal chat-completions stream.

``FIRST_STREAM_EVENT_TIMEOUT_SECONDS`` stops guarding the moment *any* transport
chunk arrives, including an empty or metadata-only one. Without a second bound a
provider that starts a stream and then goes silent left the loop polling an
empty queue forever: no ``Done``, no ``ApiError``, no terminal state, and a run
the UI keeps showing as live.

``CHAT_INTER_EVENT_TIMEOUT_SECONDS`` bounds that gap. These tests inject tiny
timeout values instead of sleeping for production durations, and assert the
timeout never fabricates completion: no ``Done``, no ``ToolCallEnd`` on a
half-streamed tool call, and cancellation is never reported as a stall.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from aura.client import chat_completions_transport as ct
from aura.client import deepseek as ds
from aura.client import responses_transport as rt
from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallEnd,
    ToolCallStart,
)

# ── Fake transport ──────────────────────────────────────────────────────


def _chunk(
    *,
    reasoning: str | None = None,
    content: str | None = None,
    tool_calls=None,
    finish_reason: str | None = None,
    usage=None,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        reasoning_content=reasoning, content=content, tool_calls=tool_calls
    )
    return SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
    )


def _metadata_only_chunk() -> SimpleNamespace:
    """A first chunk carrying no choices, no usage — pure stream startup."""
    return SimpleNamespace(usage=None, choices=[])


def _tool_call_fragment(index: int, call_id: str, name: str, args: str):
    return [
        SimpleNamespace(
            index=index,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=args),
        )
    ]


class _Stream:
    """Yields the scripted chunks, then either ends or goes silent forever."""

    def __init__(self, chunks: list, *, stall: bool = False, raises: Exception | None = None):
        self._chunks = chunks
        self._stall = stall
        self._raises = raises
        self._release = threading.Event()
        self.closed = False
        self.close_calls = 0

    def __iter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._raises is not None:
            raise self._raises
        if self._stall:
            # Silent provider: the pump thread parks here, so the consumer's
            # queue stays empty with no sentinel and no error.
            self._release.wait(30)

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self._release.set()

    def release(self) -> None:
        self._release.set()


def _client(stream: _Stream) -> ds.DeepSeekClient:
    """A DeepSeekClient wired to the fake transport, no network or key."""
    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = "deepseek"
    client._api_key = "test"
    client._base_url = "https://api.deepseek.com"
    client._chat_protocol = "openai_chat"
    client._requires_reasoning_replay = True
    client._timeout = SimpleNamespace(connect=10.0, read=None)
    client._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: stream)
        )
    )
    return client


def _run(client: ds.DeepSeekClient, **kwargs) -> list:
    return list(
        client.stream(
            messages=[{"role": "user", "content": "go"}],
            tools=None,
            # These tests cover the retained legacy Chat Completions watchdog;
            # direct V4 production streaming is covered by the Responses tests.
            model="deepseek-chat",
            thinking="high",
            **kwargs,
        )
    )


@pytest.fixture
def released():
    """Ensures every stalled fake stream is released after the test."""
    streams: list[_Stream] = []
    yield streams.append
    for stream in streams:
        stream.release()


def _api_errors(events: list) -> list[ApiError]:
    return [e for e in events if isinstance(e, ApiError)]


# ── First-event bound (existing behavior, unchanged) ────────────────────


def test_no_first_chunk_triggers_the_first_event_timeout(monkeypatch, released) -> None:
    monkeypatch.setattr(ct, "FIRST_STREAM_EVENT_TIMEOUT_SECONDS", 0.0)
    stream = _Stream([], stall=True)
    released(stream)

    events = _run(_client(stream))

    errors = _api_errors(events)
    assert len(errors) == 1
    assert "first response chunk" in errors[0].message
    assert not [e for e in events if isinstance(e, Done)]
    assert stream.close_calls == 1


# ── Inter-event bound (the repair) ──────────────────────────────────────


def test_repeated_chunks_keep_the_stream_alive(monkeypatch) -> None:
    """Liveness is per-chunk: many chunks under an aggressive bound still run to
    a normal terminal sentinel."""
    monkeypatch.setattr(ct, "CHAT_INTER_EVENT_TIMEOUT_SECONDS", 0.0)
    chunks = [_metadata_only_chunk()]
    chunks += [_chunk(content=f"part{i}") for i in range(12)]
    chunks.append(_chunk(finish_reason="stop"))
    stream = _Stream(chunks)

    events = _run(_client(stream))

    assert _api_errors(events) == []
    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert done[0].finish_reason == "stop"
    assert "".join(e.text for e in events if isinstance(e, ContentDelta)) == "".join(
        f"part{i}" for i in range(12)
    )


def test_empty_first_chunk_then_silence_trips_the_inter_event_timeout(
    monkeypatch, released
) -> None:
    """The exact hole: a metadata-only chunk disarmed the first-event watchdog."""
    monkeypatch.setattr(ct, "FIRST_STREAM_EVENT_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(ct, "CHAT_INTER_EVENT_TIMEOUT_SECONDS", 0.0)
    stream = _Stream([_metadata_only_chunk()], stall=True)
    released(stream)

    events = _run(_client(stream))

    errors = _api_errors(events)
    assert len(errors) == 1
    assert "stalled after starting" in errors[0].message
    assert "no model output was received" in errors[0].message
    assert errors[0].status_code is None
    assert not [e for e in events if isinstance(e, Done)]
    assert stream.closed


def test_meaningful_reasoning_then_silence_reports_a_stall_without_completion(
    monkeypatch, released
) -> None:
    monkeypatch.setattr(ct, "CHAT_INTER_EVENT_TIMEOUT_SECONDS", 0.0)
    stream = _Stream([_chunk(reasoning="thinking about the fix")], stall=True)
    released(stream)

    events = _run(_client(stream))

    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == [
        "thinking about the fix"
    ]
    errors = _api_errors(events)
    assert len(errors) == 1
    assert "stalled after starting" in errors[0].message
    assert "partial output was received" in errors[0].message
    # No fabricated completion of any kind.
    assert not [e for e in events if isinstance(e, Done)]


def test_half_streamed_tool_call_is_not_completed_by_the_timeout(
    monkeypatch, released
) -> None:
    """An incomplete tool call must never be presented as ready to execute."""
    monkeypatch.setattr(ct, "CHAT_INTER_EVENT_TIMEOUT_SECONDS", 0.0)
    stream = _Stream(
        [_chunk(tool_calls=_tool_call_fragment(0, "call-1", "read_file", '{"path":'))],
        stall=True,
    )
    released(stream)

    events = _run(_client(stream))

    assert [type(e) for e in events if isinstance(e, ToolCallStart)] == [ToolCallStart]
    assert not [e for e in events if isinstance(e, ToolCallEnd)]
    assert not [e for e in events if isinstance(e, Done)]
    assert len(_api_errors(events)) == 1


def test_cancellation_wins_over_the_timeout(monkeypatch, released) -> None:
    """A user stop is not a provider stall and must not be reported as one."""
    monkeypatch.setattr(ct, "CHAT_INTER_EVENT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(ct, "FIRST_STREAM_EVENT_TIMEOUT_SECONDS", 0.0)
    stream = _Stream([], stall=True)
    released(stream)
    cancel = threading.Event()
    cancel.set()

    events = _run(_client(stream), cancel_event=cancel)

    assert _api_errors(events) == []
    assert stream.close_calls == 1


def test_cancellation_closes_once_and_keeps_the_consumed_partial_message(released) -> None:
    stream = _Stream([_chunk(content="partial")], stall=True)
    released(stream)
    cancel = threading.Event()
    events = iter(
        _client(stream).stream(
            messages=[{"role": "user", "content": "go"}],
            tools=None,
            model="deepseek-chat",
            thinking="high",
            cancel_event=cancel,
        )
    )

    first = next(events)
    assert isinstance(first, ContentDelta)
    assert first.text == "partial"

    cancel.set()
    remaining = list(events)

    assert stream.close_calls == 1
    assert _api_errors(remaining) == []
    done = [event for event in remaining if isinstance(event, Done)]
    assert len(done) == 1
    assert done[0].full_message["content"] == "partial"


def test_closing_the_event_iterator_closes_the_created_stream_once(released) -> None:
    stream = _Stream([_chunk(content="partial")], stall=True)
    released(stream)
    events = _client(stream).stream(
        messages=[{"role": "user", "content": "go"}],
        tools=None,
        model="deepseek-chat",
        thinking="high",
    )

    assert isinstance(next(events), ContentDelta)
    events.close()

    assert stream.close_calls == 1


def test_terminal_sentinel_completes_normally(monkeypatch) -> None:
    monkeypatch.setattr(ct, "CHAT_INTER_EVENT_TIMEOUT_SECONDS", 0.0)
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_cache_hit_tokens=64,
        prompt_cache_miss_tokens=36,
    )
    stream = _Stream(
        [_chunk(content="answer", finish_reason="stop"), _chunk(usage=usage)]
    )

    events = _run(_client(stream))

    assert _api_errors(events) == []
    done = [e for e in events if isinstance(e, Done)]
    assert len(done) == 1
    assert done[0].full_message["content"] == "answer"
    assert stream.close_calls == 1


def test_producer_exception_still_surfaces(monkeypatch) -> None:
    monkeypatch.setattr(ct, "CHAT_INTER_EVENT_TIMEOUT_SECONDS", 0.0)
    stream = _Stream([_chunk(content="partial")], raises=RuntimeError("connection reset"))

    events = _run(_client(stream))

    errors = _api_errors(events)
    assert len(errors) == 1
    assert "connection reset" in errors[0].message
    assert "stalled after starting" not in errors[0].message
    assert not [e for e in events if isinstance(e, Done)]
    assert stream.close_calls == 1


def test_inter_event_constant_is_its_own_value() -> None:
    """Not accidentally the Responses/web-search bound, whose semantics and
    value are a different contract."""
    assert ct.CHAT_INTER_EVENT_TIMEOUT_SECONDS != rt.RESPONSES_INTER_EVENT_TIMEOUT_SECONDS
    assert ct.CHAT_INTER_EVENT_TIMEOUT_SECONDS >= ct.FIRST_STREAM_EVENT_TIMEOUT_SECONDS
