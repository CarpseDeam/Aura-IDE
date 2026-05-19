"""Anthropic/Claude streaming adapter — separate from the OpenAI-compatible client."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import httpx

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
from aura.config import ThinkingMode


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
