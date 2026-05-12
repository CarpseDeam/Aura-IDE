"""APIAgentBackend — wraps DeepSeekClient as an AgentBackend."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

from aura.backends.base import AgentBackend
from aura.client.deepseek import DeepSeekClient
from aura.client.events import Event
from aura.config import ProviderId, ThinkingMode


class APIAgentBackend(AgentBackend):
    """Agent backend that delegates to DeepSeekClient (OpenAI-compatible API).

    This is the default backend, wrapping the existing API client logic.
    It supports all providers (deepseek, openai, anthropic, google, openrouter)
    via the `provider` parameter.
    """

    def __init__(self, provider: ProviderId = "deepseek") -> None:
        self._client = DeepSeekClient(provider=provider)

    @property
    def client(self) -> DeepSeekClient:
        """Access the underlying DeepSeekClient (used by bridge internals)."""
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
        return self._client.stream(
            messages=messages,
            tools=tools,
            model=model,
            thinking=thinking,
            cancel_event=cancel_event,
            temperature=temperature,
        )
