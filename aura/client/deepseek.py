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

import httpx
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
        self._api_key = key
        self._base_url = cfg.base_url.rstrip("/")
        # Use a generous timeout with read=None to avoid [WinError 10054] / ReadError
        # during long thinking/streaming sessions. The OpenAI client will manage
        # its own connection pool.
        timeout = httpx.Timeout(120.0, connect=10.0, read=None)
        self._client = OpenAI(
            api_key=key,
            base_url=cfg.base_url,
            timeout=timeout,
            max_retries=3,
        )

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
            if self._provider == "anthropic":
                cfg = get_provider("anthropic")
                return [{"id": mid} for mid in cfg.models]
            if self._provider == "openrouter":
                # OpenRouter provides a richer metadata endpoint
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
        if self._provider == "anthropic":
            yield from _stream_anthropic(
                api_key=self._api_key,
                base_url=self._base_url,
                messages=messages,
                tools=tools,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
                temperature=temperature,
            )
            return

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


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(msg.get("tool_call_id", "")),
                            "content": str(msg.get("content") or ""),
                        }
                    ],
                }
            )
            continue
        if role not in ("user", "assistant"):
            continue

        content_blocks: list[dict[str, Any]] = []
        
        # 1. Handle Thinking (Reasoning)
        rc = msg.get("reasoning_content")
        if rc and isinstance(rc, str):
            content_blocks.append({"type": "thinking", "thinking": rc})

        # 2. Handle Content (Text/Images)
        content = msg.get("content")
        if isinstance(content, str):
            if content:
                content_blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text = part.get("text")
                    if text:
                        content_blocks.append({"type": "text", "text": text})
                elif ptype == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        # data:image/png;base64,iVBOR...
                        try:
                            header, data = url.split(",", 1)
                            media_type = header.split(":", 1)[1].split(";", 1)[0]
                            content_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": data,
                                }
                            })
                        except Exception:
                            continue

        # 3. Handle Tool Calls
        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    if not isinstance(fn, dict):
                        continue
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        tool_input = json.loads(raw_args)
                    except json.JSONDecodeError:
                        tool_input = {}
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": str(tc.get("id", "")),
                            "name": str(fn.get("name", "")),
                            "input": tool_input,
                        }
                    )

        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})
        converted.append({"role": role, "content": content_blocks})

    return ("\n\n".join(system_parts) if system_parts else None), converted


def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        converted.append(
            {
                "name": name,
                "description": str(fn.get("description") or ""),
                "input_schema": fn.get("parameters") or {"type": "object"},
            }
        )
    return converted


def _anthropic_max_tokens(model: str, thinking: ThinkingMode) -> int:
    if thinking == "off":
        return 8192
    if model in {"claude-opus-4-7", "claude-opus-4-6"}:
        return 32768
    return 20000 if thinking == "high" else 36000


def _anthropic_thinking_config(model: str, thinking: ThinkingMode) -> dict[str, Any]:
    if model in {"claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6"}:
        return {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": "high" if thinking == "high" else "max"},
        }
    budget = 10000 if thinking == "high" else 32000
    return {
        "thinking": {
            "type": "enabled",
            "budget_tokens": budget,
            "display": "summarized",
        }
    }


def _iter_anthropic_sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines.clear()
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())


def _merge_anthropic_usage(target: dict[str, int], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    input_tokens = int(raw.get("input_tokens") or 0)
    cache_read = int(raw.get("cache_read_input_tokens") or 0)
    cache_creation = int(raw.get("cache_creation_input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    if input_tokens:
        target["prompt_tokens"] = input_tokens
        target["cache_hit_tokens"] = cache_read
        target["cache_miss_tokens"] = max(0, input_tokens - cache_read) + cache_creation
    if output_tokens:
        target["completion_tokens"] = output_tokens


def _finalize_anthropic_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    arguments = tool_call["function"].get("arguments") or "{}"
    try:
        json.loads(arguments)
    except json.JSONDecodeError:
        arguments = "{}"
    tool_call["function"]["arguments"] = arguments
    return tool_call

def _stream_anthropic(
    api_key: str,
    base_url: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str,
    thinking: ThinkingMode,
    cancel_event: threading.Event | None,
    temperature: float,
) -> Iterator[Event]:
    system, anthropic_messages = _to_anthropic_messages(messages)
    body: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": _anthropic_max_tokens(model, thinking),
        "stream": True,
    }
    if system:
        body["system"] = system
    anthropic_tools = _to_anthropic_tools(tools or [])
    if anthropic_tools:
        body["tools"] = anthropic_tools
    if thinking == "off":
        body["temperature"] = temperature
    else:
        body.update(_anthropic_thinking_config(model, thinking))

    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "x-api-key": api_key,
    }

    content_buf: list[str] = []
    reasoning_buf: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    seen_tool_starts: set[int] = set()
    finish_reason: str | None = None
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
    }

    # Use a generous timeout with read=None to avoid [WinError 10054] / ReadError
    # during long thinking/streaming sessions.
    timeout = httpx.Timeout(120.0, connect=10.0, read=None)
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                f"{base_url}/messages",
                headers=headers,
                json=body,
            ) as response:
                response.raise_for_status()
                for event in _iter_anthropic_sse(response):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    ev_type = event.get("type")

                    if ev_type == "message_start":
                        _merge_anthropic_usage(usage, event.get("message", {}).get("usage"))
                        continue
                    if ev_type == "message_delta":
                        delta = event.get("delta") or {}
                        finish_reason = delta.get("stop_reason") or finish_reason
                        _merge_anthropic_usage(usage, event.get("usage"))
                        continue
                    if ev_type == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            index = int(event.get("index", 0))
                            tool_calls[index] = {
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input") or {}),
                                },
                            }
                            seen_tool_starts.add(index)
                            yield ToolCallStart(
                                index=index,
                                id=tool_calls[index]["id"],
                                name=tool_calls[index]["function"]["name"],
                            )
                        continue
                    if ev_type == "content_block_delta":
                        index = int(event.get("index", 0))
                        delta = event.get("delta") or {}
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                content_buf.append(text)
                                yield ContentDelta(text)
                        elif delta_type == "thinking_delta":
                            text = delta.get("thinking") or ""
                            if text:
                                reasoning_buf.append(text)
                                yield ReasoningDelta(text)
                        elif delta_type == "input_json_delta":
                            chunk = delta.get("partial_json") or ""
                            if chunk:
                                slot = tool_calls.setdefault(
                                    index,
                                    {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                slot["function"]["arguments"] += chunk
                                if index in seen_tool_starts:
                                    yield ToolCallArgsDelta(index=index, args_chunk=chunk)
                        continue
                    if ev_type == "content_block_stop":
                        index = int(event.get("index", 0))
                        if index in seen_tool_starts:
                            yield ToolCallEnd(index=index)
                        continue
                    if ev_type == "error":
                        error = event.get("error") or {}
                        yield ApiError(
                            status_code=None,
                            message=str(error.get("message") or error),
                        )
                        return
    except httpx.HTTPStatusError as exc:
        yield ApiError(status_code=exc.response.status_code, message=str(exc))
        return
    except Exception as exc:
        yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
        return

    if any(usage.values()):
        yield Usage(**usage)

    full_message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_buf),
        "reasoning_content": "".join(reasoning_buf),
    }
    if not full_message["reasoning_content"]:
        full_message.pop("reasoning_content")
    if tool_calls:
        full_message["tool_calls"] = [
            _finalize_anthropic_tool_call(tool_calls[i])
            for i in sorted(tool_calls)
        ]

    yield Done(finish_reason=finish_reason, full_message=full_message)
