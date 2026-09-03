"""Focused capacity tests for local OpenAI-compatible inference."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from aura.backends import api


class _BlockingClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any):
        self.calls.append(kwargs)
        self.started.set()
        if not self.release.wait(2.0):
            raise AssertionError("test stream was not released")
        return
        yield  # pragma: no cover - makes this a generator


def _backend(provider: str, client: _BlockingClient) -> api.APIAgentBackend:
    backend = api.APIAgentBackend(provider=provider)  # type: ignore[arg-type]
    backend._client = client
    return backend


def _consume(
    backend: api.APIAgentBackend,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    list(
        backend.stream(
            messages=[{"role": "user", "content": "go"}],
            tools=None,
            model="test-model",
            thinking="off",
            cancel_event=cancel_event,
        )
    )


def _mark_test_provider_local(monkeypatch) -> None:
    real_has = api.provider_registry.has
    real_get = api.provider_registry.get
    monkeypatch.setattr(
        api.provider_registry,
        "has",
        lambda provider: provider == "test_local" or real_has(provider),
    )
    monkeypatch.setattr(
        api.provider_registry,
        "get",
        lambda provider: (
            SimpleNamespace(kind="local")
            if provider == "test_local"
            else real_get(provider)
        ),
    )


def test_local_streams_share_one_slot_while_hosted_streams_remain_ungated(
    monkeypatch,
) -> None:
    _mark_test_provider_local(monkeypatch)
    first = _BlockingClient()
    second = _BlockingClient()
    hosted = _BlockingClient()
    threads = [
        threading.Thread(target=_consume, args=(_backend("test_local", first),)),
        threading.Thread(target=_consume, args=(_backend("test_local", second),)),
        threading.Thread(target=_consume, args=(_backend("deepseek", hosted),)),
    ]

    threads[0].start()
    assert first.started.wait(1.0)
    threads[1].start()
    threads[2].start()
    try:
        assert hosted.started.wait(1.0)
        assert not second.started.wait(0.15)
        first.release.set()
        assert second.started.wait(1.0)
    finally:
        first.release.set()
        second.release.set()
        hosted.release.set()
        for thread in threads:
            thread.join(2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert len(first.calls) == len(second.calls) == len(hosted.calls) == 1


def test_cancelled_local_waiter_exits_without_starting_a_stream(monkeypatch) -> None:
    _mark_test_provider_local(monkeypatch)
    holder = _BlockingClient()
    waiter = _BlockingClient()
    holder_thread = threading.Thread(
        target=_consume,
        args=(_backend("test_local", holder),),
    )
    cancel = threading.Event()
    waiter_started = threading.Event()

    def consume_waiter() -> None:
        waiter_started.set()
        _consume(_backend("test_local", waiter), cancel_event=cancel)

    waiter_thread = threading.Thread(target=consume_waiter)
    holder_thread.start()
    assert holder.started.wait(1.0)
    waiter_thread.start()
    assert waiter_started.wait(1.0)
    try:
        assert not waiter.started.wait(0.15)
        cancel.set()
        waiter_thread.join(1.0)
        assert not waiter_thread.is_alive()
        assert waiter.calls == []
    finally:
        holder.release.set()
        waiter.release.set()
        holder_thread.join(2.0)
        waiter_thread.join(2.0)

    assert not holder_thread.is_alive()
