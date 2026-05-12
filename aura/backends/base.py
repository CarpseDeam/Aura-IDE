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

    Optionally override `check_auth()` and `run_cli_auth()` for CLI-based
    backends that require interactive authentication.
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

    def check_auth(self) -> bool:
        """Return True if the backend is authenticated and ready to use.

        Default implementation returns True (API-based backends are always
        considered authenticated as long as an API key is configured).

        CLI-based backends should override this to probe actual credential
        state (e.g., by running a token check subprocess).
        """
        return True

    def run_cli_auth(self) -> bool:
        """Run an interactive CLI authentication flow.

        Default implementation is a no-op that returns True (API backends
        do not need CLI auth). Override in CLIAgentBackend subclasses.

        Returns:
            True if authentication succeeded, False otherwise.
        """
        return True
