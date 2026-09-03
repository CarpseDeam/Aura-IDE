"""APIAgentBackend for native and OpenAI-compatible API providers."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

from aura.backends.base import AgentBackend
from aura.client.events import Event
from aura.config import ProviderId, ThinkingMode
from aura.providers.registry import provider_registry

_LOCAL_STREAM_CAPACITY = threading.Semaphore(1)
_LOCAL_STREAM_CAPACITY_POLL_SECONDS = 0.05


def _acquire_local_stream_capacity(
    cancel_event: threading.Event | None,
) -> bool:
    """Wait for the single local inference slot without swallowing cancellation."""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        if not _LOCAL_STREAM_CAPACITY.acquire(
            timeout=_LOCAL_STREAM_CAPACITY_POLL_SECONDS
        ):
            continue
        if cancel_event is not None and cancel_event.is_set():
            _LOCAL_STREAM_CAPACITY.release()
            return False
        return True


class APIAgentBackend(AgentBackend):
    """Agent backend for API providers using the OpenAI-compatible client."""

    def __init__(self, provider: ProviderId = "deepseek") -> None:
        self._provider = provider
        self._client = None

    @property
    def client(self):
        """Access the underlying provider client."""
        if self._client is None:
            self._client = provider_registry.create_client(self._provider)
        return self._client

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        request: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": model,
            "thinking": thinking,
            "cancel_event": cancel_event,
            "temperature": temperature,
        }
        is_local = (
            provider_registry.has(self._provider)
            and provider_registry.get(self._provider).kind == "local"
        )
        if not is_local:
            yield from self.client.stream(**request)
            return

        if not _acquire_local_stream_capacity(cancel_event):
            return
        try:
            # Capacity covers only model-stream consumption. The slot is released
            # before AgentLoop can execute any requested tools or child work.
            yield from self.client.stream(**request)
        finally:
            _LOCAL_STREAM_CAPACITY.release()
