"""Transport execution for the two native Responses protocols."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

from openai import APIError, APIStatusError

from aura.client.deepseek_responses import DeepSeekResponsesStreamParser
from aura.client.events import ApiError, Done, Event
from aura.client.responses_common import translate_to_responses_tools
from aura.client.responses_web_search import ResponsesStreamParser

_log = logging.getLogger(__name__)

FIRST_STREAM_EVENT_TIMEOUT_SECONDS = 60.0
RESPONSES_INTER_EVENT_TIMEOUT_SECONDS = 30.0
_RESPONSES_POLL_SECONDS = 0.1


def stream_deepseek_responses(
    *,
    client: Any,
    request: dict[str, Any],
    model: str,
    thinking: Any = "high",
    cancel_event: threading.Event | None = None,
) -> Iterator[Event]:
    """Dispatch and consume one DeepSeek V4 Responses stream."""
    _log.info(
        "deepseek_responses_start model=%s thinking=%s input_items=%d "
        "tool_count=%d tool_choice=%s previous_response_id=%s conversation=%s",
        model,
        thinking,
        len(request.get("input", [])),
        len(request.get("tools", [])),
        request.get("tool_choice", "<none>"),
        "previous_response_id" in request,
        "conversation" in request,
    )
    try:
        stream = client.responses.create(**request)
    except APIStatusError as exc:
        yield ApiError(status_code=exc.status_code, message=str(exc))
        return
    except APIError as exc:
        yield ApiError(status_code=None, message=str(exc))
        return
    except Exception as exc:  # network errors, ssl, etc.
        yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
        return

    chunk_queue: queue.Queue = queue.Queue()

    def _pump_stream() -> None:
        try:
            for event in stream:
                chunk_queue.put(("event", event))
            chunk_queue.put(("sentinel", None))
        except Exception as exc:  # noqa: BLE001
            chunk_queue.put(("error", exc))

    pump_thread = threading.Thread(target=_pump_stream, daemon=True)
    pump_thread.start()
    parser = DeepSeekResponsesStreamParser()
    first_read = True
    wait_started = time.time()

    def _close_stream_quietly() -> None:
        closer = getattr(stream, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:  # noqa: BLE001
            _log.debug("deepseek_responses_stream_close_failed")

    while True:
        if cancel_event is not None and cancel_event.is_set():
            _close_stream_quietly()
            parser.cancel()
            break

        budget = (
            FIRST_STREAM_EVENT_TIMEOUT_SECONDS
            if first_read
            else RESPONSES_INTER_EVENT_TIMEOUT_SECONDS
        )
        try:
            kind, payload = chunk_queue.get(timeout=_RESPONSES_POLL_SECONDS)
        except queue.Empty:
            elapsed = time.time() - wait_started
            if elapsed <= budget:
                continue
            stage = "first" if first_read else "next"
            _log.info(
                "deepseek_responses_stream_timeout model=%s stage=%s elapsed_ms=%d",
                model,
                stage,
                int(elapsed * 1000),
            )
            _close_stream_quietly()
            parser.fail(
                f"DeepSeek Responses stream stalled: no {stage} event within "
                f"{int(budget)} seconds",
                code="stream_timeout",
            )
            break

        if first_read:
            first_read = False
            wait_started = time.time()
        else:
            wait_started = time.time()

        if kind == "sentinel":
            break
        if kind == "error":
            exc = payload
            if isinstance(exc, APIStatusError):
                yield ApiError(status_code=exc.status_code, message=str(exc))
            elif isinstance(exc, APIError):
                yield ApiError(status_code=None, message=str(exc))
            else:
                yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
            return

        try:
            events = parser.push(payload)
        except Exception as exc:  # noqa: BLE001
            yield ApiError(
                status_code=None,
                message=f"DeepSeek Responses stream parse error: {type(exc).__name__}: {exc}",
            )
            return
        yield from events
        if parser.terminal:
            break

    if cancel_event is not None and cancel_event.is_set():
        parser.cancel()
        yield Done(
            finish_reason="cancelled",
            full_message=parser.full_message(include_tool_calls=False),
        )
        return

    if parser.status in {"failed", "incomplete"}:
        yield ApiError(status_code=None, message=parser.failure_message())
        return
    if not parser.settled:
        yield ApiError(
            status_code=None,
            message=(
                "DeepSeek Responses stream ended without a terminal response. "
                "No tool calls were executed."
            ),
        )
        return

    yield Done(
        finish_reason=parser.finish_reason,
        full_message=parser.full_message(),
    )


def stream_native_web_search(
    *,
    client: Any,
    provider: str,
    request: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> Iterator[Event]:
    """Dispatch and consume one native Responses web-search stream."""
    _log.info(
        "responses_web_search_start provider=%s model=%s tools=%s",
        provider,
        request["model"],
        translate_to_responses_tools(request.get("tools")),
    )

    try:
        stream = client.responses.create(**request)
    except APIStatusError as exc:
        yield ApiError(status_code=exc.status_code, message=str(exc))
        return
    except APIError as exc:
        yield ApiError(status_code=None, message=str(exc))
        return
    except Exception as exc:  # network errors, ssl, etc.
        yield ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")
        return

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
    first_read = True
    wait_started = time.time()

    while True:
        if cancel_event is not None and cancel_event.is_set():
            parser.cancel()
            break

        budget = (
            FIRST_STREAM_EVENT_TIMEOUT_SECONDS
            if first_read
            else RESPONSES_INTER_EVENT_TIMEOUT_SECONDS
        )
        try:
            kind, payload = chunk_queue.get(timeout=_RESPONSES_POLL_SECONDS)
        except queue.Empty:
            elapsed = time.time() - wait_started
            if elapsed <= budget:
                continue
            stage = "first" if first_read else "next"
            _log.info(
                "responses_web_search_timeout provider=%s stage=%s elapsed_ms=%d",
                provider,
                stage,
                int(elapsed * 1000),
            )
            parser.fail(
                f"web search stream stalled: no {stage} response event within "
                f"{int(budget)} seconds",
                code="stream_timeout",
            )
            break

        first_read = False
        wait_started = time.time()

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
        yield from events
        if parser.terminal:
            break

    if cancel_event is not None and cancel_event.is_set():
        parser.cancel()
    elif not parser.settled:
        parser.fail(
            "web search stream ended without a terminal response",
            code="incomplete_stream",
        )
    yield Done(finish_reason=parser.finish_reason, full_message=parser.finish())
