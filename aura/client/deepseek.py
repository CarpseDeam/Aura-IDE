"""Streaming DeepSeek (and generic OpenAI-compatible) client.

Yields events; never raises. The reasoning parameters for one request come from
:func:`aura.client.reasoning.resolve_reasoning_request`, which maps the user's
``off · auto · high · max`` selection onto:

- DeepSeek:   extra_body={"thinking":...}, plus reasoning_effort only when the
              user explicitly chose high/max — ``auto`` enables thinking and
              omits reasoning_effort so DeepSeek makes its own native choice.
- OpenAI etc: reasoning_effort at top level for explicit high/max; omitted for
              ``auto`` so the provider applies its documented default.

Anthropic requests are handled by ``aura.client.anthropic_stream``, which uses
the native adaptive thinking mode.
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

# Maximum gap between two Responses stream events before the search is
# declared dead. Exceeding it produces a terminal failed result, never a
# silent "still in progress" one.
RESPONSES_INTER_EVENT_TIMEOUT_SECONDS = 30.0

# Cancellation poll interval while waiting on the Responses stream queue.
# Short enough that a cancel during an active search is observed promptly.
_RESPONSES_POLL_SECONDS = 0.1


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

        reasoning = resolve_reasoning_request(self._provider, thinking)
        if reasoning.extra_body is not None:
            kwargs["extra_body"] = reasoning.extra_body
        if reasoning.reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning.reasoning_effort
        if reasoning.send_temperature:
            kwargs["temperature"] = temperature

        _log.info(
            "provider_stream_start provider=%s model=%s thinking=%s "
            "reasoning_effort=%s effort_sent=%s effort_policy=%s "
            "base_url_host=%s timeout_connect=%s timeout_read=%s",
            self._provider, model, thinking,
            reasoning.reasoning_effort or "<omitted>",
            reasoning.effort_sent,
            reasoning.effort_policy,
            urlparse(self._base_url).hostname,
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
    _anthropic_max_tokens,
    _anthropic_thinking_config,
    _finalize_anthropic_tool_call,
    _iter_anthropic_sse,
    _merge_anthropic_usage,
    _stream_anthropic,
    _to_anthropic_messages,
    _to_anthropic_tools,
)
