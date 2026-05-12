"""Abstract base class for all agent backends."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from aura.config import ThinkingMode
from aura.client.events import Event


class AgentBackend(ABC):
    """Interface for AI model backends.

    Every backend must implement `stream()`, which yields events as the model
    generates a response. This is the only method the conversation loop needs.
    """

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        """Stream a model response, yielding Event objects.

        Args:
            messages: The conversation history in API format.
            tools: Tool definitions for function-calling, or None.
            model: Model identifier string.
            thinking: Thinking mode ('off', 'high', 'max').
            cancel_event: Optional event — when set, the stream should
                          stop generating as soon as possible.
            temperature: Sampling temperature (0.0-2.0).

        Yields:
            Event instances (ContentDelta, ReasoningDelta, ToolCallStart,
            ToolCallArgsDelta, ToolCallEnd, Usage, Done, ApiError).
        """
        ...
