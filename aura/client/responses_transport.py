"""Shared transport execution for production Responses protocols."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

from openai import APIError, APIStatusError

from aura.client.deepseek_responses import ResponsesProductionStreamParser
from aura.client.events import ApiError, Done, Event

_log = logging.getLogger(__name__)

FIRST_STREAM_EVENT_TIMEOUT_SECONDS = 60.0
RESPONSES_INTER_EVENT_TIMEOUT_SECONDS = 30.0
_RESPONSES_POLL_SECONDS = 0.1


def stream_responses(
    *,
    client: Any,
    request: dict[str, Any],
    provider: str,
    model: str,
    thinking: Any = "high",
    hosted_tool_type: str = "",
    cancel_event: threading.Event | None = None,
) -> Iterator[Event]:
    """Dispatch and consume one selected-provider production Responses stream."""
    _log.info(
        "responses_start provider=%s model=%s thinking=%s input_items=%d "
        "tool_count=%d tool_choice=%s previous_response_id=%s conversation=%s",
        provider,
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
    parser = ResponsesProductionStreamParser(
        provider=provider,
        hosted_tool_type=hosted_tool_type,
    )
    first_read = True
    wait_started = time.time()

    def _close_stream_quietly() -> None:
        closer = getattr(stream, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:  # noqa: BLE001
            _log.debug("responses_stream_close_failed provider=%s", provider)

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
                "responses_stream_timeout provider=%s model=%s stage=%s elapsed_ms=%d",
                provider,
                model,
                stage,
                int(elapsed * 1000),
            )
            _close_stream_quietly()
            parser.fail(
                f"{provider} Responses stream stalled: no {stage} event within "
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
                message=f"{provider} Responses stream parse error: {type(exc).__name__}: {exc}",
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
                f"{provider} Responses stream ended without a terminal response. "
                "No tool calls were executed."
            ),
        )
        return

    yield from parser.emit_citation_suffix()
    yield Done(
        finish_reason=parser.finish_reason,
        full_message=parser.full_message(),
    )
