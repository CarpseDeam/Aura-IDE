"""Streaming DeepSeek (and generic OpenAI-compatible) client.

Yields events; never raises. The reasoning parameters for one request come from
:func:`aura.client.reasoning.resolve_reasoning_request`, which maps the user's
``off · auto · high · max`` selection onto:

- DeepSeek OpenAI-compatible chat: extra_body={"thinking":...}, plus
  reasoning_effort only when the user explicitly chose high/max — ``auto``
  enables thinking and omits reasoning_effort so DeepSeek makes its own native
  choice.
- OpenAI etc: reasoning_effort at top level for explicit high/max; omitted for
  ``auto`` so the provider applies its documented default.

DeepSeek production chat/tool turns use this OpenAI-compatible path: DeepSeek's
``chat_protocol`` is ``openai_chat``, so they go to DeepSeek's official Chat
Completions API — ``POST https://api.deepseek.com/chat/completions`` — with
``stream=True`` and ``stream_options={"include_usage": True}``.  Native
Anthropic keeps ``anthropic_messages`` and its own transport.  The transport is
chosen from the provider's chat metadata here — never inferred from the
provider id.

Canonical history is already in OpenAI shape (``system``/``user``/
``assistant``/``tool``, with ``tool_calls`` and ``tool_call_id``), so
``History.for_api`` output is sent as-is: an assistant message that carried tool
calls replays its complete ``reasoning_content``, ``content``, and
``tool_calls``, and each tool result carries the matching ``tool_call_id``.

One DeepSeek thinking-mode rule is enforced here rather than trusted to callers,
because it is rejected with a 400 rather than degraded: a thinking-enabled
request must replay ``reasoning_content`` on every assistant message after the
last user message, so the trailing chain is filled in where Aura honestly
produced none (see ``_ensure_reasoning_replay``).  It is gated on the provider's
``requires_reasoning_replay`` metadata, not on the provider id, so OpenAI and
OpenRouter — which do not accept the field — are untouched.

It is request-local: the user's saved selection is never rewritten, canonical
history is never touched, and it logs what it changed.
"""
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

import httpx
from openai import APIError, APIStatusError, OpenAI

_log = logging.getLogger(__name__)
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
from aura.client.responses_stream import (
    ResponsesStreamParser,
    build_native_web_search_request,
    translate_to_responses_tools,
)
from aura.config import (
    ProviderId,
    ThinkingMode,
    get_provider,
    resolve_api_key,
)

FIRST_STREAM_EVENT_TIMEOUT_SECONDS = 60.0

# Maximum gap between two chat-completions transport chunks once the stream has
# started. The first-event watchdog above stops guarding the moment any chunk
# arrives — including an empty or metadata-only one — so without this a provider
# that goes silent mid-stream leaves the run polling forever and the UI Live.
# Deliberately its own constant, and conservative: a thinking model can be slow
# between chunks, and this is a stall detector, not a latency budget.
CHAT_INTER_EVENT_TIMEOUT_SECONDS = 180.0

# Maximum gap between two Responses stream events before the search is
# declared dead. Exceeding it produces a terminal failed result, never a
# silent "still in progress" one.
RESPONSES_INTER_EVENT_TIMEOUT_SECONDS = 30.0

# Cancellation poll interval while waiting on the Responses stream queue.
# Short enough that a cancel during an active search is observed promptly.
_RESPONSES_POLL_SECONDS = 0.1

#: Stands in for ``reasoning_content`` on an assistant message that genuinely
#: never had any.  DeepSeek requires the field on every assistant message after
#: the last ``role=user`` message while thinking is enabled, and rejects the
#: request outright without it — 400 "The `reasoning_content` in the thinking
#: mode must be passed back to the API".  Aura produces such messages honestly:
#: the decision checkpoint and the focused action must run with thinking off, a
#: dispatched worker's synthetic assistant turn never had reasoning, and a
#: conversation reloaded from disk or replayed after a provider switch can carry
#: assistant turns from a model that never emitted any.  The placeholder says
#: what is true rather than inventing reasoning the model did not do.
REASONING_REPLAY_PLACEHOLDER = "[No reasoning was recorded for this step.]"


#: Keys that canonical history may legitimately carry but that mean nothing on
#: the Chat Completions wire.  ``reasoning_signature`` is produced only by the
#: Anthropic transport; a conversation started on Anthropic and continued on
#: DeepSeek would otherwise ship it as an unknown field.
_FOREIGN_MESSAGE_KEYS = ("reasoning_signature",)


def _strip_foreign_message_keys(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return *messages* without keys belonging to another wire protocol.

    Request-local and non-destructive, like ``_ensure_reasoning_replay``:
    messages needing no change are passed through by reference and canonical
    history is never touched.  Only foreign keys are removed — ``content``,
    ``reasoning_content``, ``tool_calls``, and ``tool_call_id`` are always kept
    so an assistant message that carried tool calls replays in full.
    """
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
    """Return *messages* with the trailing assistant chain safe to replay.

    DeepSeek's thinking-mode rule binds the assistant messages *after* the last
    ``role=user`` message — the chain it is being asked to continue. Ones before
    that boundary may omit ``reasoning_content`` freely, which is what lets the
    outbound view shed superseded reasoning at all, so only the chain is filled
    in and the token savings elsewhere are kept.

    Request-local and non-destructive: the caller's list and every message it
    does not have to touch are passed through by reference, and canonical
    history never sees the placeholder. Returns the list and how many messages
    were filled in, so the caller can log it.
    """
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
        # Ordinary API root — used for model discovery, pricing/catalog work,
        # native Responses web search, and every non-chat path.
        self._base_url = cfg.base_url.rstrip("/")
        # Chat transport metadata, read once here. The chat protocol decides
        # the wire transport for chat/tool turns; the chat API root overrides
        # ``base_url`` for chat only. Nothing else infers the protocol from the
        # provider id.
        self._chat_protocol = cfg.chat_protocol
        self._chat_base_url = (cfg.chat_base_url or cfg.base_url).rstrip("/")
        self._requires_reasoning_replay = bool(cfg.requires_reasoning_replay)
        # Use a generous timeout with read=None to avoid [WinError 10054] / ReadError
        # during long thinking/streaming sessions. The OpenAI client will manage
        # its own connection pool.
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
        # Chat transport is a provider-metadata property, not a provider-name
        # check. DeepSeek (and native Anthropic) speak Anthropic Messages;
        # OpenAI, OpenRouter, and any other ``openai_chat`` provider fall
        # through to the OpenAI-compatible path below. There is no silent
        # fallback: if the Anthropic endpoint fails, the real provider error is
        # surfaced by the adapter, never replaced with a Chat Completions retry.
        if self._chat_protocol == "anthropic_messages":
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
            )
            return

        # Canonical history is already OpenAI-shaped, so it is sent as-is apart
        # from keys that belong to the other transport and mean nothing here.
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
            kwargs["tool_choice"] = "auto"

        # The user's selected thinking mode is the mode that gets sent. Nothing
        # here overrides it.
        effective_thinking: ThinkingMode = thinking

        # A thinking-enabled DeepSeek request must replay ``reasoning_content``
        # on every assistant message after the last user message, and Aura
        # honestly produces messages without it — workers synthesize assistant
        # turns, and a reloaded conversation can predate the current selection.
        # Filling the chain here means the mode the user picked is the mode that
        # gets sent, instead of a 400.
        if self._requires_reasoning_replay and effective_thinking != "off":
            kwargs["messages"], filled = _ensure_reasoning_replay(outbound)
            if filled:
                _log.info(
                    "deepseek_reasoning_replay_filled model=%s thinking=%s "
                    "messages_filled=%d placeholder_chars=%d",
                    model, effective_thinking, filled,
                    len(REASONING_REPLAY_PLACEHOLDER),
                )

        reasoning = resolve_reasoning_request(self._provider, effective_thinking)
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
            self._provider, self._chat_protocol,
            urlparse(self._base_url).hostname,
            model, effective_thinking,
            thinking, effective_thinking,
            self._requires_reasoning_replay,
            kwargs.get("tool_choice", "<none>"),
            kwargs.get("parallel_tool_calls", "<default>"),
            reasoning.reasoning_effort or "<omitted>",
            reasoning.effort_sent,
            reasoning.effort_policy,
            self._timeout.connect, self._timeout.read,
        )
        try:
            from aura.updater import is_packaged
            _log.info("provider_stream_start packaged=%s", is_packaged())
        except ImportError:
            pass
        try:
            import certifi
            _certifi_path = certifi.where()
            _log.info(
                "provider_stream_start certifi_path=%s "
                "certifi_file_exists=%s "
                "SSL_CERT_FILE=%s REQUESTS_CA_BUNDLE=%s",
                _certifi_path,
                os.path.exists(_certifi_path),
                "<set>" if "SSL_CERT_FILE" in os.environ else "<not set>",
                "<set>" if "REQUESTS_CA_BUNDLE" in os.environ else "<not set>",
            )
        except ImportError:
            _log.info("provider_stream_start certifi=not_available")

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
        dsml_parser = DsmlParser(start_index=1000)

        def _yield_dsml_events(events: Iterator[Event]) -> Iterator[Event]:
            for event in events:
                if isinstance(event, ContentDelta):
                    content_buf.append(event.text)
                yield event

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

        # Queue+pump-daemon pattern guards against silent hangs when the
        # provider never sends the first streaming chunk.
        _log.info(
            "provider_stream_first_event_wait_start provider=%s model=%s timeout_s=%s",
            self._provider, model, FIRST_STREAM_EVENT_TIMEOUT_SECONDS,
        )

        chunk_queue: queue.Queue = queue.Queue()

        def _pump_stream() -> None:
            try:
                for chunk in stream:
                    chunk_queue.put(('chunk', chunk))
                chunk_queue.put(('sentinel', None))
            except Exception as exc:  # noqa: BLE001
                chunk_queue.put(('error', exc))

        pump_thread = threading.Thread(target=_pump_stream, daemon=True)
        pump_thread.start()

        _first_event_start = time.time()
        _first_read = True
        # Time of the most recent transport chunk of any kind, and whether the
        # stream has produced anything the caller can act on. Both feed the
        # inter-event watchdog's report so a stall is diagnosable.
        _last_chunk_at = _first_event_start
        _meaningful_emitted = False

        def _close_stream_quietly() -> None:
            """Best-effort release of the underlying HTTP stream on timeout."""
            closer = getattr(stream, "close", None)
            if closer is None:
                return
            try:
                closer()
            except Exception:  # noqa: BLE001 — teardown must not mask the timeout
                _log.debug("provider_stream_close_failed provider=%s", self._provider)

        while True:
            if cancel_event is not None and cancel_event.is_set():
                break

            try:
                if _first_read:
                    kind, value = chunk_queue.get(timeout=0.1)
                else:
                    kind, value = chunk_queue.get(timeout=0.5)
            except queue.Empty:
                if _first_read:
                    elapsed = time.time() - _first_event_start
                    if elapsed > FIRST_STREAM_EVENT_TIMEOUT_SECONDS:
                        _log.info(
                            "provider_stream_first_event_timeout provider=%s model=%s "
                            "elapsed_ms=%d base_url_host=%s",
                            self._provider, model,
                            int(elapsed * 1000),
                            urlparse(self._base_url).hostname,
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

                # Stream started, then went silent. Terminate instead of polling
                # forever: no Done is fabricated, no partial tool call is
                # completed, and every tool result already produced this turn
                # stays exactly as it was recorded.
                stalled_for = time.time() - _last_chunk_at
                if stalled_for > CHAT_INTER_EVENT_TIMEOUT_SECONDS:
                    _log.info(
                        "provider_stream_inter_event_timeout provider=%s model=%s "
                        "elapsed_since_last_chunk_ms=%d meaningful_output=%s "
                        "metadata_only_stream=%s base_url_host=%s",
                        self._provider, model,
                        int(stalled_for * 1000),
                        _meaningful_emitted,
                        not _meaningful_emitted,
                        urlparse(self._base_url).hostname,
                    )
                    _close_stream_quietly()
                    yield ApiError(
                        status_code=None,
                        message=(
                            f"Provider stream stalled after starting: no further "
                            f"response chunk for "
                            f"{int(CHAT_INTER_EVENT_TIMEOUT_SECONDS)} seconds "
                            f"({'partial output was received' if _meaningful_emitted else 'no model output was received'})."
                            f" The turn is incomplete; completed tool results are "
                            f"preserved. Retry when the provider is healthy."
                        ),
                    )
                    return
                continue

            if kind == 'sentinel':
                break
            if kind == 'error':
                exc = value
                if isinstance(exc, APIStatusError):
                    yield ApiError(status_code=exc.status_code, message=str(exc))
                elif isinstance(exc, APIError):
                    yield ApiError(status_code=None, message=str(exc))
                else:
                    yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
                return

            # kind == 'chunk'
            chunk = value
            # Any chunk counts as liveness, including an empty or metadata-only
            # one — that is exactly the case the first-event watchdog stops
            # covering.
            _last_chunk_at = time.time()

            if _first_read:
                _first_read = False
                elapsed_ms = int((time.time() - _first_event_start) * 1000)
                _log.info(
                    "provider_stream_first_event provider=%s model=%s elapsed_ms=%d",
                    self._provider, model, elapsed_ms,
                )

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
                _meaningful_emitted = True
            if (
                getattr(delta, "reasoning_content", None)
                or delta.content
                or delta.tool_calls
            ):
                _meaningful_emitted = True

            # Reasoning text (CoT) — the thinking-mode field.
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_buf.append(rc)
                yield ReasoningDelta(rc)

            # Final answer text.
            if delta.content:
                yield from _yield_dsml_events(dsml_parser.push(delta.content))

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

        yield from _yield_dsml_events(dsml_parser.flush())

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
        parsed_tool_calls = dsml_parser.get_tool_calls()
        if tool_calls or parsed_tool_calls:
            full_message["tool_calls"] = [
                tool_calls[i] for i in sorted(tool_calls)
            ] + parsed_tool_calls
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

    def stream_responses_web_search(
        self,
        question: str,
        context: str | None = None,
        model: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[Event]:
        """Stream a native Responses API web search as normalized Aura events.

        Builds the DeepSeek native request::

            client.responses.create(
                model=<provider default or model>,
                input=[{"role": "user", "content": question + context}],
                tools=[{"type": "web_search"}],
                tool_choice={"type": "web_search"},
                stream=True,
            )

        ``tool_choice`` names the built-in explicitly so an Aura ``web_search``
        call always searches instead of being answered from model memory.

        Yields ContentDelta/Usage events and a final ``Done`` whose
        ``full_message`` is the neutral research payload (status, text,
        sources, usage, error).  On API failures yields ``ApiError``.  The
        ``Done`` status is always terminal — ``completed``, ``incomplete``,
        ``failed``, or ``cancelled``.  ``cancel_event`` is the caller's turn
        cancel event; this method never creates one of its own.
        """
        cfg = get_provider(self._provider)
        request_kwargs = build_native_web_search_request(
            question=question,
            context=context,
            model=model or cfg.default_model,
        )

        _log.info(
            "responses_web_search_start provider=%s model=%s tools=%s",
            self._provider,
            request_kwargs["model"],
            translate_to_responses_tools(request_kwargs.get("tools")),
        )

        try:
            stream = self._client.responses.create(**request_kwargs)
        except APIStatusError as exc:
            yield ApiError(status_code=exc.status_code, message=str(exc))
            return
        except APIError as exc:
            yield ApiError(status_code=None, message=str(exc))
            return
        except Exception as exc:  # network errors, ssl, etc.
            yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
            return

        # Queue+pump-daemon pattern guards against silent hangs when the
        # provider never sends the first streaming chunk.
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

        parser = ResponsesStreamParser()
        _first_read = True
        _wait_started = time.time()

        while True:
            if cancel_event is not None and cancel_event.is_set():
                parser.cancel()
                break

            budget = (
                FIRST_STREAM_EVENT_TIMEOUT_SECONDS
                if _first_read
                else RESPONSES_INTER_EVENT_TIMEOUT_SECONDS
            )
            try:
                kind, payload = chunk_queue.get(timeout=_RESPONSES_POLL_SECONDS)
            except queue.Empty:
                elapsed = time.time() - _wait_started
                if elapsed <= budget:
                    continue
                stage = "first" if _first_read else "next"
                _log.info(
                    "responses_web_search_timeout provider=%s stage=%s elapsed_ms=%d",
                    self._provider, stage, int(elapsed * 1000),
                )
                parser.fail(
                    f"web search stream stalled: no {stage} response event within "
                    f"{int(budget)} seconds",
                    code="stream_timeout",
                )
                break

            _first_read = False
            _wait_started = time.time()

            if kind == "sentinel":
                break
            if kind == "error":
                yield ApiError(
                    status_code=None,
                    message=f"{type(payload).__name__}: {payload}",
                )
                return

            try:
                events = parser.push(payload)
            except Exception as exc:  # noqa: BLE001
                yield ApiError(
                    status_code=None,
                    message=f"responses stream parse error: {type(exc).__name__}: {exc}",
                )
                return
            for event in events:
                yield event
            if parser.terminal:
                break

        # Cancellation wins over anything the stream said on its way out, and a
        # stream that stopped without a terminal status is a failure — never a
        # Done that still claims "in_progress".
        if cancel_event is not None and cancel_event.is_set():
            parser.cancel()
        elif not parser.settled:
            parser.fail(
                "web search stream ended without a terminal response",
                code="incomplete_stream",
            )
        yield Done(finish_reason=parser.finish_reason, full_message=parser.finish())


# ---------------------------------------------------------------------------
# Backward-compat re-exports (Anthropic streaming helpers moved to their own module)
# ---------------------------------------------------------------------------
from aura.client.anthropic_stream import (  # noqa: E402, F401
    AnthropicThinkingProfile,
    _anthropic_max_tokens,
    _anthropic_thinking_config,
    _finalize_anthropic_tool_call,
    _iter_anthropic_sse,
    _merge_anthropic_usage,
    _stream_anthropic,
    _to_anthropic_messages,
    _to_anthropic_tools,
    anthropic_thinking_profile,
)
