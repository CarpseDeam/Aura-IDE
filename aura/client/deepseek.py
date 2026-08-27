"""Stable DeepSeekClient facade for provider setup and transport selection."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import httpx
from openai import OpenAI

from aura.client.anthropic_stream import _stream_anthropic
from aura.client.chat_completions_transport import stream_chat_completions
from aura.client.deepseek_responses import build_responses_request
from aura.client.events import Event
from aura.client.responses_transport import stream_responses
from aura.config import (
    ProviderId,
    ThinkingMode,
    get_provider,
    resolve_api_key,
)
from aura.providers.native_search import native_web_search_capability


class DeepSeekClient:
    """Wrap a configured provider endpoint as an Aura event stream.

    The class name and public methods are retained for backward compatibility.
    It owns provider setup, model discovery, and the protocol selection boundary;
    focused transport modules own request execution and parsing.
    """

    def __init__(
        self,
        api_key: str | None = None,
        provider: ProviderId = "deepseek",
    ) -> None:
        self._provider = provider
        cfg = get_provider(provider)
        key = api_key if api_key is not None else resolve_api_key(provider)
        self._api_key = key
        self._base_url = cfg.base_url.rstrip("/")
        self._chat_protocol = cfg.chat_protocol
        self._chat_base_url = (cfg.chat_base_url or cfg.base_url).rstrip("/")
        self._requires_reasoning_replay = bool(cfg.requires_reasoning_replay)
        self._timeout = httpx.Timeout(120.0, connect=10.0, read=None)
        self._client = OpenAI(
            api_key=key,
            base_url=cfg.base_url,
            timeout=self._timeout,
            max_retries=3,
        )

    @property
    def provider(self) -> ProviderId:
        return self._provider

    def list_models(self) -> list[str]:
        """Fetch the list of available models from the provider's API."""
        try:
            models = self._client.models.list()
            return [model.id for model in models]
        except Exception:
            return []

    def fetch_raw_models(self) -> list[dict[str, Any]]:
        """Fetch raw provider model objects for catalog and pricing work."""
        try:
            if self._provider == "anthropic":
                cfg = get_provider("anthropic")
                return [{"id": model_id} for model_id in cfg.models]
            if self._provider == "openrouter":
                response = httpx.get("https://openrouter.ai/api/v1/models", timeout=10.0)
                response.raise_for_status()
                return response.json().get("data", [])

            models = self._client.models.list(timeout=10.0)
            return [model.model_dump() for model in models]
        except Exception:
            return []

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        """Select the configured protocol and delegate the stream execution."""
        if self._uses_responses(model):
            capability = native_web_search_capability(
                self._provider,
                model,
                transport="responses",
            )
            request = build_responses_request(
                provider=self._provider,
                messages=messages,
                tools=tools,
                hosted_tools=[capability.tool] if capability is not None else None,
                model=model,
                thinking=thinking,
                temperature=temperature,
            )
            yield from stream_responses(
                client=self._client,
                request=request,
                provider=self._provider,
                model=model,
                thinking=thinking,
                hosted_tool_type=(
                    str(capability.tool.get("type") or "")
                    if capability is not None
                    else ""
                ),
                cancel_event=cancel_event,
            )
            return

        if self._chat_protocol == "anthropic_messages":
            capability = native_web_search_capability(
                self._provider,
                model,
                transport="anthropic_messages",
            )
            yield from _stream_anthropic(
                api_key=self._api_key,
                base_url=self._chat_base_url,
                messages=messages,
                tools=tools,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
                temperature=temperature,
                provider=self._provider,
                requires_reasoning_replay=self._requires_reasoning_replay,
                hosted_search_tool=(
                    capability.tool if capability is not None else None
                ),
            )
            return

        capability = native_web_search_capability(
            self._provider,
            model,
            transport="openai_chat",
        )
        yield from stream_chat_completions(
            client=self._client,
            provider=self._provider,
            chat_protocol=self._chat_protocol,
            base_url=self._base_url,
            timeout=self._timeout,
            messages=messages,
            tools=tools,
            model=model,
            thinking=thinking,
            cancel_event=cancel_event,
            temperature=temperature,
            requires_reasoning_replay=self._requires_reasoning_replay,
            hosted_search_tool=(
                capability.tool if capability is not None else None
            ),
        )

    def _uses_responses(self, model: str) -> bool:
        """Return whether this selected production turn uses Responses."""
        return (
            self._provider == "openai"
            or (
                self._provider == "deepseek"
                and str(model).lower().startswith("deepseek-v4-")
            )
        )
