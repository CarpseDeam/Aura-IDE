"""APIAgentBackend for native and OpenAI-compatible API providers."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

from aura.backends.base import AgentBackend
from aura.client.events import Event
from aura.config import ProviderId, ThinkingMode
from aura.providers.registry import provider_registry


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
        return self.client.stream(
            messages=messages,
            tools=tools,
            model=model,
            thinking=thinking,
            cancel_event=cancel_event,
            temperature=temperature,
        )
