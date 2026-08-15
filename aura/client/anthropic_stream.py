"""Anthropic/Claude streaming adapter — separate from the OpenAI-compatible client.

Also serves any provider that speaks the Anthropic Messages wire protocol over
its own transport — currently DeepSeek, whose Anthropic-compatible endpoint
carries the same ``thinking`` / ``tool_use`` block vocabulary. Provider-specific
behavior lives in one profile object (:class:`AnthropicThinkingProfile`), chosen
by a single seam (:func:`anthropic_thinking_profile`), so the stream loop has no
``provider == ...`` branches.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
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
)
from aura.config import ThinkingMode
from aura.providers.base import normalize_thinking_mode
from aura.providers.registry import provider_registry

_log = logging.getLogger(__name__)


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
    *,
    reconstruct_thinking: bool = True,
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    index = 0
    while index < len(messages):
        msg = messages[index]
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                system_parts.append(content)
            index += 1
            continue
        if role == "tool":
            # Every consecutive tool result goes into ONE user message: the
            # wire format requires each ``tool_use`` to be answered by a
            # ``tool_result`` in the immediately following message, and Aura
            # stores each result as its own ``role=tool`` message (two calls
            # in one turn → two tool messages). One user message per tool
            # message would leave every ``tool_use`` after the first
            # unanswered in the next message, and the provider rejects the
            # request for that (400).
            tool_results: list[dict[str, Any]] = []
            while (
                index < len(messages) and messages[index].get("role") == "tool"
            ):
                tool_msg = messages[index]
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(tool_msg.get("tool_call_id", "")),
                        "content": str(tool_msg.get("content") or ""),
                    }
                )
                index += 1
            converted.append({"role": "user", "content": tool_results})
            continue
        if role not in ("user", "assistant"):
            index += 1
            continue

        content_blocks: list[dict[str, Any]] = []

        # 1. Handle Thinking (Reasoning). A provider that continues extended
        #    thinking across tool rounds needs the round's own thinking back:
        #    the api view has already decided *which* reasoning survives (the
        #    current real user turn's), and this only re-encodes what is there
        #    into the wire representation. Reasoning the view dropped cannot
        #    reappear here, and no placeholder thinking block is ever invented.
        #    The provider's signature travels with its block when the stream
        #    reported one; an unsigned block is sent as-is rather than faked.
        rc = msg.get("reasoning_content")
        if reconstruct_thinking and rc and isinstance(rc, str):
            thinking_block: dict[str, Any] = {"type": "thinking", "thinking": rc}
            signature = msg.get("reasoning_signature")
            if isinstance(signature, str) and signature:
                thinking_block["signature"] = signature
            content_blocks.append(thinking_block)

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
        index += 1

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
    mode = normalize_thinking_mode(thinking) or "high"
    if mode == "off":
        return EFFORT_OMITTED_DISABLED
    return EFFORT_EXPLICIT


def _anthropic_max_tokens(model: str, thinking: ThinkingMode) -> int:
    mode = normalize_thinking_mode(thinking) or "high"
    if mode == "off":
        return 8192
    if model in {"claude-opus-4-7", "claude-opus-4-6"}:
        return 32768
    return 20000 if mode == "high" else 36000


def _anthropic_thinking_config(model: str, thinking: ThinkingMode) -> dict[str, Any]:
    mode = normalize_thinking_mode(thinking) or "high"
    if mode == "off":
        # Native Anthropic keeps its existing shape: no thinking field at all,
        # with ``temperature`` sent instead by the caller. Unchanged behavior.
        return {}
    if model in _ADAPTIVE_THINKING_MODELS:
        return {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": mode},
        }
    budget = 10000 if mode == "high" else 32000
    return {
        "thinking": {
            "type": "enabled",
            "budget_tokens": budget,
            "display": "summarized",
        }
    }


#: Fallback output budget when a DeepSeek model is not in the provider catalog.
_DEEPSEEK_ANTHROPIC_MAX_TOKENS_FALLBACK: int = 32_000

# Bounded liveness for the Anthropic-Messages stream. These mirror the
# watchdog in `aura.client.deepseek` for the chat-completions transport: the
# provider gets 60s to send the first SSE event (the HTTP response may arrive
# while the model is still cold-starting its thinking phase), and 180s between
# events once the stream is live (a thinking model can legitimately be slow
# between chunks — this is a stall detector, not a latency budget). Without
# these, a provider that accepts the request and then holds the connection
# open — no events, no close — leaves the worker thread blocked on the socket
# forever and the run "live" in the UI, exactly what the chat path was
# protected from and the Anthropic path was not.
FIRST_STREAM_EVENT_TIMEOUT_SECONDS: float = 60.0
CHAT_INTER_EVENT_TIMEOUT_SECONDS: float = 180.0

#: SSE pump poll interval while no event is in flight. Short enough that a
#: cancel or a stall is observed promptly.
_ANTHROPIC_SSE_POLL_SECONDS: float = 0.1


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

    DeepSeek ignores ``budget_tokens``, so it is never sent. Explicit
    ``high``/``max`` selections send that effort verbatim.

    ``off`` is stated explicitly as ``{"type": "disabled"}`` rather than by
    omitting the field: an absent ``thinking`` selects whatever the endpoint
    defaults to for the model, which for a reasoning model is not necessarily
    off. The user's Off selection has to be a request, not a silence. The
    caller still sends ``temperature`` in this mode.
    """
    mode = normalize_thinking_mode(thinking) or "high"
    if mode == "off":
        return {"thinking": {"type": "disabled"}}
    config: dict[str, Any] = {"thinking": {"type": "enabled"}}
    config["output_config"] = {"effort": mode}
    return config


def _deepseek_anthropic_effort_policy(model: str, thinking: ThinkingMode) -> str:
    """Why the effort was sent or omitted, for the DeepSeek Anthropic log line."""
    mode = normalize_thinking_mode(thinking) or "high"
    if mode == "off":
        return EFFORT_OMITTED_DISABLED
    return EFFORT_EXPLICIT


@dataclass(frozen=True)
class AnthropicThinkingProfile:
    """Provider-aware thinking policy for the Anthropic Messages transport.

    One object owns how a provider maps the user's ``off · high · max``
    onto the Anthropic ``thinking`` / ``output_config`` fields, what output
    budget to allow, and whether prior assistant ``reasoning_content`` in the
    outbound view is reconstructed as ``thinking`` blocks.

    The user's explicit high/max selection is always stated verbatim.
    """

    provider: str
    #: Whether assistant ``reasoning_content`` in the outbound copy is
    #: reconstructed as Anthropic ``thinking`` blocks. Every provider on this
    #: transport continues its own thinking across a tool round, so the
    #: reasoning the api view kept (the current real user turn's) is re-encoded
    #: for the wire. It never invents reasoning: what the view dropped stays
    #: dropped.
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


def anthropic_thinking_profile(
    provider: str, *, reconstruct_thinking: bool = True
) -> AnthropicThinkingProfile:
    """The one decision seam: pick an Anthropic Messages thinking profile.

    Native Anthropic keeps its Claude-specific adaptive/budget thinking;
    DeepSeek uses its native thinking over the same transport. Whether prior
    reasoning is re-encoded as ``thinking`` blocks is the provider's declared
    ``requires_reasoning_replay``, threaded in by the caller rather than
    inferred from the provider id here.
    """
    return AnthropicThinkingProfile(
        provider=provider, reconstruct_thinking=reconstruct_thinking
    )


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
    # Anthropic reports `input_tokens` as the ordinary uncached input only.
    # `cache_read_input_tokens` is served from cache (a hit); newly cached input
    # in `cache_creation_input_tokens` was still processed this turn (a miss).
    # The three are disjoint, so total prompt input is their sum.
    if input_tokens or cache_read or cache_creation:
        target["cache_hit_tokens"] = cache_read
        target["cache_miss_tokens"] = input_tokens + cache_creation
        target["prompt_tokens"] = input_tokens + cache_read + cache_creation
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
    provider: str = "anthropic",
    requires_reasoning_replay: bool = True,
) -> Iterator[Event]:
    profile = anthropic_thinking_profile(
        provider, reconstruct_thinking=requires_reasoning_replay
    )
    # With thinking disabled the request declares no thinking at all, so prior
    # thinking blocks have nothing to continue and are not sent; the assistant's
    # own ``content`` carries the continuity for that mode.
    system, anthropic_messages = _to_anthropic_messages(
        messages,
        reconstruct_thinking=profile.reconstruct_thinking and thinking != "off",
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
        # No ``tool_choice``: the model chooses freely between prose and tool
        # calls, and parallel tool use is left enabled.
        body["tools"] = anthropic_tools
    if thinking == "off":
        body["temperature"] = temperature
    # Off is a request too: a provider whose profile has an explicit
    # disabled-thinking representation sends it, rather than leaving the field
    # out and inheriting whatever the endpoint defaults to.
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
    signature_buf: list[str] = []
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
    # during long thinking/streaming sessions. Liveness is enforced below by
    # the first-event/inter-event watchdog around the SSE pump, not by socket
    # timeouts — a held-open connection surfaces as a terminal ApiError instead
    # of a silent forever-block on ``iter_lines``.
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

                sse_queue: queue.Queue = queue.Queue()

                def _pump_sse() -> None:
                    """Feed SSE events into ``sse_queue`` off the main loop."""
                    try:
                        for ev in _iter_anthropic_sse(response):
                            sse_queue.put(("event", ev))
                        sse_queue.put(("sentinel", None))
                    except Exception as exc:  # noqa: BLE001 — surfaced as ApiError
                        sse_queue.put(("error", exc))

                pump_thread = threading.Thread(target=_pump_sse, daemon=True)
                pump_thread.start()

                _first_event_at = time.monotonic()
                _first_read = True
                _last_event_at = _first_event_at

                def _close_stream_quietly() -> None:
                    """Best-effort release of the HTTP stream on a stall."""
                    closer = getattr(response, "close", None)
                    if closer is None:
                        return
                    try:
                        closer()
                    except Exception:  # noqa: BLE001 — teardown must not mask the timeout
                        _log.debug("anthropic_stream_close_failed host=%s", urlparse(base_url).hostname)

                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        break

                    try:
                        if _first_read:
                            kind, payload = sse_queue.get(timeout=_ANTHROPIC_SSE_POLL_SECONDS)
                        else:
                            kind, payload = sse_queue.get(timeout=0.5)
                    except queue.Empty:
                        if _first_read:
                            elapsed = time.monotonic() - _first_event_at
                            if elapsed > FIRST_STREAM_EVENT_TIMEOUT_SECONDS:
                                _log.info(
                                    "anthropic_stream_first_event_timeout host=%s model=%s "
                                    "elapsed_ms=%d",
                                    urlparse(base_url).hostname, model,
                                    int(elapsed * 1000),
                                )
                                yield ApiError(
                                    status_code=None,
                                    message=(
                                        f"Provider did not send a first response event within "
                                        f"{int(FIRST_STREAM_EVENT_TIMEOUT_SECONDS)} seconds. "
                                        f"Check connection, provider status, model availability, "
                                        f"or inspect the local logs."
                                    ),
                                )
                                return
                            continue

                        # Stream started, then went silent. Terminate instead of
                        # polling forever: no Done is fabricated and no partial
                        # tool call is completed, matching the chat path.
                        stalled_for = time.monotonic() - _last_event_at
                        if stalled_for > CHAT_INTER_EVENT_TIMEOUT_SECONDS:
                            _log.info(
                                "anthropic_stream_inter_event_timeout host=%s model=%s "
                                "elapsed_since_last_event_ms=%d",
                                urlparse(base_url).hostname, model,
                                int(stalled_for * 1000),
                            )
                            _close_stream_quietly()
                            yield ApiError(
                                status_code=None,
                                message=(
                                    f"Provider stream stalled after starting: no further "
                                    f"response event for {int(CHAT_INTER_EVENT_TIMEOUT_SECONDS)} "
                                    f"seconds. The turn is incomplete; completed tool results "
                                    f"are preserved. Retry when the provider is healthy."
                                ),
                            )
                            return
                        continue

                    if kind == "sentinel":
                        break
                    if kind == "error":
                        yield ApiError(
                            status_code=None,
                            message=f"{type(payload).__name__}: {payload}",
                        )
                        return

                    event = payload
                    _first_read = False
                    _last_event_at = time.monotonic()
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
                            block_input = block.get("input")
                            tool_calls[index] = {
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    # The block's ``input`` at start is the
                                    # partial JSON object parsed so far —
                                    # ``{}`` on a streamed call, whose real
                                    # arguments arrive as ``input_json_delta``
                                    # fragments appended below. Seed only a
                                    # non-empty input (a server that sends the
                                    # full input up front); never seed "{}" or
                                    # the fragments would be appended onto it
                                    # and the result would not parse.
                                    "arguments": (
                                        json.dumps(block_input)
                                        if block_input
                                        else ""
                                    ),
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
                        elif delta_type == "signature_delta":
                            # The provider's own signature over the thinking it
                            # just streamed. Kept so the block can be replayed
                            # intact on the next round; never displayed, never
                            # fabricated when the provider sends none.
                            sig = delta.get("signature") or ""
                            if sig:
                                signature_buf.append(sig)
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
    elif signature_buf:
        full_message["reasoning_signature"] = "".join(signature_buf)
    if tool_calls:
        full_message["tool_calls"] = [
            _finalize_anthropic_tool_call(tool_calls[i])
            for i in sorted(tool_calls)
        ]

    yield Done(finish_reason=finish_reason, full_message=full_message)
