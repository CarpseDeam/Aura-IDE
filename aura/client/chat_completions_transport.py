"""Legacy OpenAI-compatible Chat Completions streaming transport."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

from openai import APIError, APIStatusError

from aura.client.dsml_parser import DsmlParser
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
from aura.client.reasoning import resolve_reasoning_request
from aura.config import ProviderId, ThinkingMode

_log = logging.getLogger(__name__)

FIRST_STREAM_EVENT_TIMEOUT_SECONDS = 60.0
CHAT_INTER_EVENT_TIMEOUT_SECONDS = 180.0
REASONING_REPLAY_PLACEHOLDER = "[No reasoning was recorded for this step.]"
_FOREIGN_MESSAGE_KEYS = ("reasoning_signature",)


def _strip_foreign_message_keys(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return messages without keys belonging to another wire protocol."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, dict) and any(key in msg for key in _FOREIGN_MESSAGE_KEYS):
            out.append({k: v for k, v in msg.items() if k not in _FOREIGN_MESSAGE_KEYS})
        else:
            out.append(msg)
    return out


def _ensure_reasoning_replay(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return messages with the trailing assistant chain safe to replay."""
    boundary = -1
    for index, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            boundary = index

    repaired: list[dict[str, Any]] = list(messages)
    filled = 0
    for index in range(boundary + 1, len(repaired)):
        msg = repaired[index]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        existing = msg.get("reasoning_content")
        if isinstance(existing, str) and existing:
            continue
        repaired[index] = {**msg, "reasoning_content": REASONING_REPLAY_PLACEHOLDER}
        filled += 1

    return (repaired if filled else messages), filled


def stream_chat_completions(
    *,
    client: Any,
    provider: ProviderId,
    chat_protocol: str,
    base_url: str,
    timeout: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str,
    thinking: ThinkingMode,
    cancel_event: threading.Event | None = None,
    temperature: float = 0.7,
    requires_reasoning_replay: bool = False,
) -> Iterator[Event]:
    """Stream the existing OpenAI-compatible Chat Completions request."""
    outbound = _strip_foreign_message_keys(messages)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": outbound,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        # The model always chooses freely between prose and tool calls, and
        # may emit a complete parallel batch. Aura never forces a tool call
        # and never disables parallel tool use.
        kwargs["tools"] = tools

    effective_thinking: ThinkingMode = thinking
    if requires_reasoning_replay and effective_thinking != "off":
        kwargs["messages"], filled = _ensure_reasoning_replay(outbound)
        if filled:
            _log.info(
                "deepseek_reasoning_replay_filled model=%s thinking=%s "
                "messages_filled=%d placeholder_chars=%d",
                model,
                effective_thinking,
                filled,
                len(REASONING_REPLAY_PLACEHOLDER),
            )

    reasoning = resolve_reasoning_request(provider, effective_thinking)
    if reasoning.extra_body is not None:
        kwargs["extra_body"] = reasoning.extra_body
    if reasoning.reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning.reasoning_effort
    if reasoning.send_temperature:
        kwargs["temperature"] = temperature

    _log.info(
        "provider_stream_start provider=%s chat_protocol=%s "
        "chat_endpoint_host=%s model=%s thinking=%s "
        "requested_thinking=%s effective_thinking=%s "
        "replay_required=%s "
        "tool_choice=%s parallel_tool_calls=%s "
        "reasoning_effort=%s effort_sent=%s effort_policy=%s "
        "timeout_connect=%s timeout_read=%s",
        provider,
        chat_protocol,
        urlparse(base_url).hostname,
        model,
        effective_thinking,
        thinking,
        effective_thinking,
        requires_reasoning_replay,
        kwargs.get("tool_choice", "<none>"),
        kwargs.get("parallel_tool_calls", "<default>"),
        reasoning.reasoning_effort or "<omitted>",
        reasoning.effort_sent,
        reasoning.effort_policy,
        timeout.connect,
        timeout.read,
    )
    try:
        from aura.updater import is_packaged

        _log.info("provider_stream_start packaged=%s", is_packaged())
    except ImportError:
        pass
    try:
        import certifi

        certifi_path = certifi.where()
        _log.info(
            "provider_stream_start certifi_path=%s "
            "certifi_file_exists=%s "
            "SSL_CERT_FILE=%s REQUESTS_CA_BUNDLE=%s",
            certifi_path,
            os.path.exists(certifi_path),
            "<set>" if "SSL_CERT_FILE" in os.environ else "<not set>",
            "<set>" if "REQUESTS_CA_BUNDLE" in os.environ else "<not set>",
        )
    except ImportError:
        _log.info("provider_stream_start certifi=not_available")

    reasoning_buf: list[str] = []
    content_buf: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    args_buffers: dict[int, list[str]] = {}
    seen_starts: set[int] = set()
    finish_reason: str | None = None
    usage_emitted = False
    dsml_parser = DsmlParser(start_index=1000)

    def _yield_dsml_events(events: Iterator[Event]) -> Iterator[Event]:
        for event in events:
            if isinstance(event, ContentDelta):
                content_buf.append(event.text)
            yield event

    try:
        stream = client.chat.completions.create(**kwargs)
    except APIStatusError as exc:
        yield ApiError(status_code=exc.status_code, message=str(exc))
        return
    except APIError as exc:
        yield ApiError(status_code=None, message=str(exc))
        return
    except Exception as exc:  # network errors, ssl, etc.
        yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
        return

    _log.info(
        "provider_stream_first_event_wait_start provider=%s model=%s timeout_s=%s",
        provider,
        model,
        FIRST_STREAM_EVENT_TIMEOUT_SECONDS,
    )

    chunk_queue: queue.Queue = queue.Queue()

    def _pump_stream() -> None:
        try:
            for chunk in stream:
                chunk_queue.put(("chunk", chunk))
            chunk_queue.put(("sentinel", None))
        except Exception as exc:  # noqa: BLE001
            chunk_queue.put(("error", exc))

    pump_thread = threading.Thread(target=_pump_stream, daemon=True)
    pump_thread.start()

    first_event_start = time.time()
    first_read = True
    last_chunk_at = first_event_start
    meaningful_emitted = False

    def _close_stream_quietly() -> None:
        """Best-effort release of the underlying HTTP stream on timeout."""
        closer = getattr(stream, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:  # noqa: BLE001
            _log.debug("provider_stream_close_failed provider=%s", provider)

    while True:
        if cancel_event is not None and cancel_event.is_set():
            break

        try:
            if first_read:
                kind, value = chunk_queue.get(timeout=0.1)
            else:
                kind, value = chunk_queue.get(timeout=0.5)
        except queue.Empty:
            if first_read:
                elapsed = time.time() - first_event_start
                if elapsed > FIRST_STREAM_EVENT_TIMEOUT_SECONDS:
                    _log.info(
                        "provider_stream_first_event_timeout provider=%s model=%s "
                        "elapsed_ms=%d base_url_host=%s",
                        provider,
                        model,
                        int(elapsed * 1000),
                        urlparse(base_url).hostname,
                    )
                    yield ApiError(
                        status_code=None,
                        message=(
                            f"Provider did not send a first response chunk within "
                            f"{int(FIRST_STREAM_EVENT_TIMEOUT_SECONDS)} seconds. "
                            f"Check connection, provider status, model availability, "
                            f"or inspect the local logs."
                        ),
                    )
                    return
                continue

            stalled_for = time.time() - last_chunk_at
            if stalled_for > CHAT_INTER_EVENT_TIMEOUT_SECONDS:
                _log.info(
                    "provider_stream_inter_event_timeout provider=%s model=%s "
                    "elapsed_since_last_chunk_ms=%d meaningful_output=%s "
                    "metadata_only_stream=%s base_url_host=%s",
                    provider,
                    model,
                    int(stalled_for * 1000),
                    meaningful_emitted,
                    not meaningful_emitted,
                    urlparse(base_url).hostname,
                )
                _close_stream_quietly()
                yield ApiError(
                    status_code=None,
                    message=(
                        f"Provider stream stalled after starting: no further "
                        f"response chunk for "
                        f"{int(CHAT_INTER_EVENT_TIMEOUT_SECONDS)} seconds "
                        f"({'partial output was received' if meaningful_emitted else 'no model output was received'})."
                        f" The turn is incomplete; completed tool results are "
                        f"preserved. Retry when the provider is healthy."
                    ),
                )
                return
            continue

        if kind == "sentinel":
            break
        if kind == "error":
            exc = value
            if isinstance(exc, APIStatusError):
                yield ApiError(status_code=exc.status_code, message=str(exc))
            elif isinstance(exc, APIError):
                yield ApiError(status_code=None, message=str(exc))
            else:
                yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
            return

        chunk = value
        last_chunk_at = time.time()

        if first_read:
            first_read = False
            elapsed_ms = int((time.time() - first_event_start) * 1000)
            _log.info(
                "provider_stream_first_event provider=%s model=%s elapsed_ms=%d",
                provider,
                model,
                elapsed_ms,
            )

        if not usage_emitted and getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
            cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            yield Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
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
            meaningful_emitted = True
        if getattr(delta, "reasoning_content", None) or delta.content or delta.tool_calls:
            meaningful_emitted = True

        reasoning_content = getattr(delta, "reasoning_content", None)
        if reasoning_content:
            reasoning_buf.append(reasoning_content)
            yield ReasoningDelta(reasoning_content)

        if delta.content:
            yield from _yield_dsml_events(dsml_parser.push(delta.content))

        if delta.tool_calls:
            for tool_call in delta.tool_calls:
                index = tool_call.index
                slot = tool_calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tool_call.id:
                    slot["id"] = tool_call.id
                if tool_call.function is not None:
                    if tool_call.function.name:
                        slot["function"]["name"] = tool_call.function.name
                    if tool_call.function.arguments:
                        slot["function"]["arguments"] += tool_call.function.arguments
                        if index not in seen_starts:
                            args_buffers.setdefault(index, []).append(tool_call.function.arguments)

                if index not in seen_starts and slot["id"] and slot["function"]["name"]:
                    seen_starts.add(index)
                    yield ToolCallStart(
                        index=index,
                        id=slot["id"],
                        name=slot["function"]["name"],
                    )
                    if index in args_buffers:
                        for fragment in args_buffers.pop(index):
                            yield ToolCallArgsDelta(index=index, args_chunk=fragment)
                elif (
                    index in seen_starts
                    and tool_call.function is not None
                    and tool_call.function.arguments
                ):
                    yield ToolCallArgsDelta(
                        index=index,
                        args_chunk=tool_call.function.arguments,
                    )

    yield from _yield_dsml_events(dsml_parser.flush())

    for index in sorted(tool_calls):
        if index in seen_starts:
            yield ToolCallEnd(index=index)

    full_message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_buf),
        "reasoning_content": "".join(reasoning_buf),
    }
    if not full_message["reasoning_content"]:
        full_message.pop("reasoning_content")
    parsed_tool_calls = dsml_parser.get_tool_calls()
    if tool_calls or parsed_tool_calls:
        full_message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)] + parsed_tool_calls
        for tool_call in full_message["tool_calls"]:
            if not tool_call["function"]["arguments"]:
                tool_call["function"]["arguments"] = "{}"
            else:
                try:
                    json.loads(tool_call["function"]["arguments"])
                except json.JSONDecodeError:
                    pass

    yield Done(finish_reason=finish_reason, full_message=full_message)
