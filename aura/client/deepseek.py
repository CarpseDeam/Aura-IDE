"""Streaming DeepSeek (and generic OpenAI-compatible) client.

Yields events; never raises. Honors thinking mode rules:
- DeepSeek:    extra_body={"thinking":...} for thinking control
- Anthropic:   extra_body={"thinking":{"type":"enabled","budget_tokens":N}} for thinking
- OpenAI/Gemini: reasoning_effort at top level; no extra_body when thinking is off
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

from openai import APIError, APIStatusError, OpenAI

from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from aura.config import (
    ProviderId,
    ThinkingMode,
    get_provider,
    resolve_api_key,
)


class DeepSeekClient:
    """Wraps an OpenAI-compatible endpoint as an event stream.

    Accepts an optional provider parameter to select the backend.
    The class name is preserved for backward compatibility.
    """

    def __init__(
        self,
        api_key: str | None = None,
        provider: ProviderId = "deepseek",
    ) -> None:
        self._provider = provider
        cfg = get_provider(provider)
        key = api_key if api_key is not None else resolve_api_key(provider)
        self._client = OpenAI(api_key=key, base_url=cfg.base_url)

    @property
    def provider(self) -> ProviderId:
        return self._provider

    def list_models(self) -> list[str]:
        """Fetch the list of available models from the provider's API."""
        try:
            models = self._client.models.list()
            return [m.id for m in models]
        except Exception:
            return []

    def fetch_raw_models(self) -> list[dict[str, Any]]:
        """Fetch the raw model objects from the provider's API.
        
        For OpenRouter, this hits their special /models endpoint which includes 
        pricing and capabilities.
        """
        try:
            if self._provider == "openrouter":
                # OpenRouter provides a richer metadata endpoint
                import httpx
                # Set a reasonable 10s timeout to prevent hanging
                resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=10.0)
                resp.raise_for_status()
                return resp.json().get("data", [])
            
            # Standard OpenAI-compatible fetch with timeout
            models = self._client.models.list(timeout=10.0)
            # Convert OpenAI model objects to dicts for uniform handling
            return [m.model_dump() for m in models]
        except Exception:
            # Silently return empty on failure, but ensure we don't hang
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
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if self._provider == "deepseek":
            if thinking == "off":
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                kwargs["temperature"] = temperature
            else:
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                kwargs["reasoning_effort"] = "high" if thinking == "high" else "max"
                # Per docs: temperature/top_p/penalties silently ignored. Skip them.
        elif self._provider == "anthropic":
            if thinking == "off":
                kwargs["temperature"] = temperature
            else:
                budget = 16000 if thinking == "high" else 32000
                kwargs["extra_body"] = {"thinking": {"type": "enabled", "budget_tokens": budget}}
        else:
            # OpenAI / Google Gemini: no extra_body thinking param.
            if thinking == "off":
                kwargs["temperature"] = temperature
            else:
                kwargs["reasoning_effort"] = "high" if thinking == "high" else "max"

        # Accumulators reproduce the streamed assistant message exactly.
        reasoning_buf: list[str] = []
        content_buf: list[str] = []
        # tool_calls indexed by stream "index" — the model can stream multiple in parallel.
        tool_calls: dict[int, dict[str, Any]] = {}
        # Buffer arguments until ToolCallStart is yielded
        args_buffers: dict[int, list[str]] = {}
        seen_starts: set[int] = set()
        finish_reason: str | None = None
        usage_emitted = False

        try:
            stream = self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            yield ApiError(status_code=exc.status_code, message=str(exc))
            return
        except APIError as exc:
            yield ApiError(status_code=None, message=str(exc))
            return
        except Exception as exc:  # network errors, ssl, etc.
            yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
            return

        try:
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    break

                # Usage may appear on a terminal-only chunk OR be bundled with the final
                # choice chunk depending on the server. Emit at most once.
                if not usage_emitted and getattr(chunk, "usage", None) is not None:
                    u = chunk.usage
                    cache_hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
                    cache_miss = getattr(u, "prompt_cache_miss_tokens", 0) or 0
                    yield Usage(
                        prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                        cache_hit_tokens=cache_hit,
                        cache_miss_tokens=cache_miss,
                    )
                    usage_emitted = True

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                # Reasoning text (CoT) — the thinking-mode field.
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    reasoning_buf.append(rc)
                    yield ReasoningDelta(rc)

                # Final answer text.
                if delta.content:
                    content_buf.append(delta.content)
                    yield ContentDelta(delta.content)

                # Tool-call fragments. OpenAI streams them as deltas keyed by index.
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        slot = tool_calls.setdefault(
                            idx,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                slot["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["function"]["arguments"] += tc.function.arguments
                                # Buffer arguments if we haven't yielded start yet
                                if idx not in seen_starts:
                                    args_buffers.setdefault(idx, []).append(tc.function.arguments)

                        if idx not in seen_starts and slot["id"] and slot["function"]["name"]:
                            seen_starts.add(idx)
                            yield ToolCallStart(
                                index=idx, id=slot["id"], name=slot["function"]["name"]
                            )
                            # Flush buffered arguments
                            if idx in args_buffers:
                                for fragment in args_buffers.pop(idx):
                                    yield ToolCallArgsDelta(index=idx, args_chunk=fragment)
                        elif idx in seen_starts and tc.function is not None and tc.function.arguments:
                            yield ToolCallArgsDelta(
                                index=idx, args_chunk=tc.function.arguments
                            )
        except APIStatusError as exc:
            yield ApiError(status_code=exc.status_code, message=str(exc))
            return
        except APIError as exc:
            yield ApiError(status_code=None, message=str(exc))
            return
        except Exception as exc:
            yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
            return

        # Close out any tool-calls we started.
        for idx in sorted(tool_calls):
            if idx in seen_starts:
                yield ToolCallEnd(index=idx)

        full_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_buf),
            "reasoning_content": "".join(reasoning_buf),
        }
        if not full_message["reasoning_content"]:
            full_message.pop("reasoning_content")
        if tool_calls:
            full_message["tool_calls"] = [
                tool_calls[i] for i in sorted(tool_calls)
            ]
            # Sanity: ensure args parse — if not, the tool runner will surface it.
            for tc in full_message["tool_calls"]:
                if not tc["function"]["arguments"]:
                    tc["function"]["arguments"] = "{}"
                else:
                    try:
                        json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        # Leave as-is; manager will catch and surface error.
                        pass

        yield Done(finish_reason=finish_reason, full_message=full_message)
