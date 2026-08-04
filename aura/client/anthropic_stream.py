"""Anthropic/Claude streaming adapter — separate from the OpenAI-compatible client.

Also serves any provider that speaks the Anthropic Messages wire protocol over
its own transport — currently DeepSeek, whose Anthropic-compatible endpoint does
not require prior reasoning to be replayed. Provider-specific behavior lives in
one profile object (:class:`AnthropicThinkingProfile`), chosen by a single
seam (:func:`anthropic_thinking_profile`), so the stream loop itself has no
``provider == ...`` branches.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

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
from aura.client.reasoning import (
    EFFORT_EXPLICIT,
    EFFORT_OMITTED_DISABLED,
    EFFORT_OMITTED_PROVIDER_AUTO,
    EFFORT_OMITTED_PROVIDER_DEFAULT,
)
from aura.config import ThinkingMode
from aura.providers.registry import provider_registry

_log = logging.getLogger(__name__)


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
    *,
    reconstruct_thinking: bool = True,
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

        # 1. Handle Thinking (Reasoning). Native Anthropic must replay prior
        #    thinking blocks to continue extended thinking. DeepSeek over the
        #    Anthropic transport does NOT reconstruct thinking blocks from
        #    ``reasoning_content``: the api view deliberately omitted that
        #    reasoning from the outbound copy, and re-injecting it here would
        #    undo the omission. No placeholder thinking blocks are ever added.
        rc = msg.get("reasoning_content")
        if reconstruct_thinking and rc and isinstance(rc, str):
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


#: Models whose API exposes the native adaptive thinking mode, where Claude
#: selects its own effort unless ``output_config.effort`` overrides it.
_ADAPTIVE_THINKING_MODELS: frozenset[str] = frozenset({
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
})


def _anthropic_effort_policy(model: str, thinking: ThinkingMode) -> str:
    """Return why the effort parameter was sent or omitted, for the log line."""
    if thinking == "off":
        return EFFORT_OMITTED_DISABLED
    if thinking != "auto":
        return EFFORT_EXPLICIT
    if model in _ADAPTIVE_THINKING_MODELS:
        return EFFORT_OMITTED_PROVIDER_AUTO
    return EFFORT_OMITTED_PROVIDER_DEFAULT


def _anthropic_max_tokens(model: str, thinking: ThinkingMode) -> int:
    if thinking == "off":
        return 8192
    if model in {"claude-opus-4-7", "claude-opus-4-6"}:
        return 32768
    # "auto" shares the high output ceiling: this is a local output cap, not a
    # reasoning instruction, and must not quietly buy a max-sized budget.
    return 20000 if thinking in {"high", "auto"} else 36000


def _anthropic_thinking_config(model: str, thinking: ThinkingMode) -> dict[str, Any]:
    if model in _ADAPTIVE_THINKING_MODELS:
        config: dict[str, Any] = {
            "thinking": {"type": "adaptive", "display": "summarized"},
        }
        if thinking != "auto":
            # Explicit user selections are always stated explicitly.
            config["output_config"] = {
                "effort": "high" if thinking == "high" else "max"
            }
        # "auto" omits output_config so adaptive thinking picks the effort.
        return config
    # Older budget-only models have no native automatic mode; "auto" therefore
    # keeps this provider's existing deterministic default rather than Aura
    # guessing at task difficulty.
    budget = 10000 if thinking in {"high", "auto"} else 32000
    return {
        "thinking": {
            "type": "enabled",
            "budget_tokens": budget,
            "display": "summarized",
        }
    }


#: Fallback output budget when a DeepSeek model is not in the provider catalog.
_DEEPSEEK_ANTHROPIC_MAX_TOKENS_FALLBACK: int = 32_000


def _catalog_output_cap(model: str) -> int:
    """Declared ``max_output_tokens`` for *model* across the catalog, or 0."""
    for spec in provider_registry.all().values():
        info = spec.models.get(model)
        if info is not None and info.max_output_tokens:
            return info.max_output_tokens
    return 0


def _deepseek_anthropic_max_tokens(model: str, thinking: ThinkingMode) -> int:
    """Output budget for a DeepSeek request over the Anthropic transport.

    Anthropic Messages requires an explicit ``max_tokens``. DeepSeek's models
    natively support a 384k-token output window (catalog ``max_output_tokens``);
    that declared capacity is sent so a coding agent has room for tool calls and
    edits. Unknown models fall back to a generous constant rather than a
    Claude-style per-mode cap.
    """
    cap = _catalog_output_cap(model)
    return cap if cap > 0 else _DEEPSEEK_ANTHROPIC_MAX_TOKENS_FALLBACK


def _deepseek_anthropic_thinking_config(model: str, thinking: ThinkingMode) -> dict[str, Any]:
    """DeepSeek thinking over the Anthropic Messages transport.

    DeepSeek ignores ``budget_tokens``, so it is never sent. ``auto`` enables
    thinking and omits ``output_config`` so DeepSeek applies its native effort
    choice; explicit ``high``/``max`` send that effort verbatim. ``off`` sends
    no thinking configuration at all (the caller handles temperature then).
    """
    if thinking == "off":
        return {}
    config: dict[str, Any] = {"thinking": {"type": "enabled"}}
    if thinking in {"high", "max"}:
        config["output_config"] = {"effort": thinking}
    return config


def _deepseek_anthropic_effort_policy(model: str, thinking: ThinkingMode) -> str:
    """Why the effort was sent or omitted, for the DeepSeek Anthropic log line."""
    if thinking == "off":
        return EFFORT_OMITTED_DISABLED
    if thinking != "auto":
        return EFFORT_EXPLICIT
    return EFFORT_OMITTED_PROVIDER_AUTO


@dataclass(frozen=True)
class AnthropicThinkingProfile:
    """Provider-aware thinking policy for the Anthropic Messages transport.

    One object owns how a provider maps the user's ``off · auto · high · max``
    onto the Anthropic ``thinking`` / ``output_config`` fields, what output
    budget to allow, and whether prior assistant ``reasoning_content`` in the
    outbound view is reconstructed as ``thinking`` blocks.

    There is deliberately no task scoring, failure counting, or escalation here:
    ``auto`` means the provider's native choice, and the user's explicit
    high/max selection is always stated verbatim.
    """

    provider: str
    #: Whether assistant ``reasoning_content`` in the outbound copy is
    #: reconstructed as Anthropic ``thinking`` blocks. Native Anthropic must
    #: replay prior thinking blocks to continue extended thinking; DeepSeek
    #: over the Anthropic transport must not rebuild blocks from reasoning the
    #: api view deliberately omitted.
    reconstruct_thinking: bool

    def max_tokens(self, model: str, thinking: ThinkingMode) -> int:
        if self.provider == "anthropic":
            return _anthropic_max_tokens(model, thinking)
        return _deepseek_anthropic_max_tokens(model, thinking)

    def thinking_config(self, model: str, thinking: ThinkingMode) -> dict[str, Any]:
        if self.provider == "anthropic":
            return _anthropic_thinking_config(model, thinking)
        return _deepseek_anthropic_thinking_config(model, thinking)

    def effort_policy(self, model: str, thinking: ThinkingMode) -> str:
        if self.provider == "anthropic":
            return _anthropic_effort_policy(model, thinking)
        return _deepseek_anthropic_effort_policy(model, thinking)


def anthropic_thinking_profile(provider: str) -> AnthropicThinkingProfile:
    """The one decision seam: pick an Anthropic Messages thinking profile.

    Native Anthropic keeps its Claude-specific adaptive/budget thinking and its
    required thinking-block reconstruction. Every other Anthropic Messages
    provider (DeepSeek) uses its native thinking over the Anthropic transport
    and must not reconstruct thinking blocks from reasoning the api view
    deliberately omitted.
    """
    if provider == "anthropic":
        return AnthropicThinkingProfile(provider=provider, reconstruct_thinking=True)
    return AnthropicThinkingProfile(provider=provider, reconstruct_thinking=False)


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
    require_tool_call: bool = False,
    provider: str = "anthropic",
    requires_reasoning_replay: bool = True,
) -> Iterator[Event]:
    profile = anthropic_thinking_profile(provider)
    system, anthropic_messages = _to_anthropic_messages(
        messages, reconstruct_thinking=profile.reconstruct_thinking
    )
    body: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": profile.max_tokens(model, thinking),
        "stream": True,
    }
    if system:
        body["system"] = system
    anthropic_tools = _to_anthropic_tools(tools or [])
    if anthropic_tools:
        body["tools"] = anthropic_tools
        if require_tool_call:
            # ``any`` means "use one of these tools" rather than naming which.
            # Anthropic rejects a forced tool choice while extended thinking is
            # on, which is exactly why the focused action request runs with
            # thinking off — the two are one decision, not two.
            body["tool_choice"] = {
                "type": "any",
                "disable_parallel_tool_use": True,
            }
    if thinking == "off":
        body["temperature"] = temperature
    else:
        body.update(profile.thinking_config(model, thinking))

    _log.info(
        "provider_stream_start provider=%s chat_protocol=anthropic_messages "
        "chat_endpoint_host=%s model=%s thinking=%s replay_required=%s "
        "tool_choice=%s reasoning_effort=%s effort_sent=%s effort_policy=%s",
        provider,
        urlparse(base_url).hostname,
        model,
        thinking,
        requires_reasoning_replay,
        json.dumps(body["tool_choice"]) if "tool_choice" in body else "<none>",
        body.get("output_config", {}).get("effort", "<omitted>"),
        "output_config" in body,
        profile.effort_policy(model, thinking),
    )

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
