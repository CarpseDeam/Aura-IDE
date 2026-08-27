"""Production Responses stream parsing and completed-message construction.

This module owns one responsibility: consuming one OpenAI-compatible
Responses event stream and building the Aura chat-shaped assistant message it
produced.  Request projection belongs to
:mod:`aura.client.responses_request`; dispatch and lifecycle belong to
:mod:`aura.client.responses_transport`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aura.client.events import (
    ContentDelta,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
)
from aura.client.hosted_search import (
    AURA_HOSTED_SEARCH_KEY,
    citation_markdown,
    citations_from_response_item,
    hosted_search_metadata,
)
from aura.client.responses_common import (
    TERMINAL_RESPONSE_STATUSES,
    TERMINAL_STREAM_STATUSES,
)
from aura.client.responses_common import (
    attr as _attr,
)
from aura.client.responses_common import (
    usage_to_events as _usage_to_events,
)
from aura.client.responses_continuation import (
    AURA_PROVIDER_REASONING_KEY,
    REASONING_CONTINUATION_PROVIDERS,
    reasoning_continuation_metadata,
    reasoning_wire_item,
)


class ResponsesProductionStreamParser:
    """Normalize one OpenAI-compatible production Responses stream.

    A function call is exposed only after its argument stream is complete;
    terminal incomplete/failed responses never produce an executable ``Done``.
    """

    def __init__(self, *, provider: str, hosted_tool_type: str = "") -> None:
        self.provider = provider
        self.hosted_tool_type = hosted_tool_type
        self.status = "in_progress"
        self.reasoning = ""
        self.text = ""
        self.response_id = ""
        self.usage: dict[str, Any] | None = None
        self.incomplete_reason: str | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.finish_reason: str | None = None
        self._usage_emitted = False
        self._calls: dict[int, dict[str, Any]] = {}
        self._item_indexes: dict[str, int] = {}
        self._reasoning_items: dict[int, dict[str, Any]] = {}
        self.citations: list[dict[str, str]] = []
        self.web_search_calls: list[dict[str, Any]] = []
        self._search_items: list[dict[str, Any]] = []
        self._seen_search_ids: set[str] = set()
        self._citations_emitted = False

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RESPONSE_STATUSES

    @property
    def settled(self) -> bool:
        return self.status in TERMINAL_STREAM_STATUSES

    @property
    def has_partial_output(self) -> bool:
        return bool(self.reasoning or self.text or self._calls)

    def cancel(self) -> None:
        self.status = "cancelled"

    def fail(self, message: str, code: str | None = None) -> None:
        self.status = "failed"
        self.error = self.error or message
        self.error_code = self.error_code or code

    def push(self, event: Any) -> list[Event]:
        """Consume one semantic Responses event and return Aura events."""
        events: list[Event] = []
        event_type = _attr(event, "type", "")

        if event_type == "response.created":
            response = _attr(event, "response")
            self.response_id = str(_attr(response, "id", "") or "")
        elif event_type in {
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        }:
            delta = str(_attr(event, "delta", "") or "")
            if delta:
                self.reasoning += delta
                events.append(ReasoningDelta(delta))
        elif event_type in {
            "response.reasoning_text.done",
            "response.reasoning_summary_text.done",
        }:
            text = str(_attr(event, "text", "") or "")
            if text:
                self.reasoning = text
        elif event_type == "response.output_text.delta":
            delta = str(_attr(event, "delta", "") or "")
            if delta:
                self.text += delta
                events.append(ContentDelta(delta))
        elif event_type == "response.output_text.done":
            text = str(_attr(event, "text", "") or "")
            if text:
                self.text = text
        elif event_type == "response.output_item.added":
            item = _attr(event, "item")
            if item is not None:
                index = self._event_index(event, item)
                self._note_hosted_item(item)
                self._note_reasoning_item(item, index)
                self._note_function_item(item, index, added=True, events=events)
        elif event_type == "response.output_item.done":
            item = _attr(event, "item")
            if item is not None:
                index = self._event_index(event, item)
                self._note_hosted_item(item)
                self._note_reasoning_item(item, index)
                self._note_function_item(item, index, added=False, events=events)
        elif event_type == "response.function_call_arguments.delta":
            index = self._event_index(event)
            delta = str(_attr(event, "delta", "") or "")
            slot = self._calls.setdefault(index, {"arguments": "", "ended": False})
            slot["arguments"] += delta
            if delta:
                events.append(ToolCallArgsDelta(index=index, args_chunk=delta))
        elif event_type == "response.function_call_arguments.done":
            index = self._event_index(event)
            arguments = str(_attr(event, "arguments", "") or "")
            slot = self._calls.setdefault(index, {"arguments": "", "ended": False})
            if arguments and slot.get("arguments") != arguments:
                if not slot.get("arguments"):
                    events.append(ToolCallArgsDelta(index=index, args_chunk=arguments))
                slot["arguments"] = arguments
            slot["complete"] = True
            self._end_call(index, events)
        elif event_type.startswith("response.web_search_call."):
            self._note_search_event(event, event_type.rsplit(".", 1)[-1])
        elif event_type == "response.completed":
            response = _attr(event, "response")
            if response is not None:
                self._finalize_completed(response, events)
        elif event_type == "response.incomplete":
            response = _attr(event, "response")
            if response is not None:
                self._set_incomplete(response, events)
        elif event_type == "response.failed":
            response = _attr(event, "response")
            if response is not None:
                self._set_failed(response)
        elif event_type == "error":
            self.status = "failed"
            self.error_code = str(_attr(event, "code", "") or "") or None
            self.error = str(_attr(event, "message", "") or "Responses API stream error")
        return events

    def full_message(self, *, include_tool_calls: bool = True) -> dict[str, Any]:
        """Return the existing Aura chat-shaped assistant message."""
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.text,
        }
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if include_tool_calls:
            calls = self._completed_tool_calls()
            if calls:
                message["tool_calls"] = calls
        metadata = hosted_search_metadata(
            provider=self.provider,
            tool_type=self.hosted_tool_type or "web_search",
            citations=self.citations,
            calls=self.web_search_calls,
        )
        if metadata is not None:
            if self._search_items:
                metadata["wire_items"] = [dict(item) for item in self._search_items]
            message[AURA_HOSTED_SEARCH_KEY] = metadata
        # Provider reasoning state is continuation state, not visible output:
        # a cancelled, incomplete, or failed response never completes it, so
        # it is stored only for a response that actually completed.
        if include_tool_calls and self.status == "completed":
            continuation = reasoning_continuation_metadata(
                provider=self.provider,
                entries=self._continuation_entries(),
            )
            if continuation is not None:
                message[AURA_PROVIDER_REASONING_KEY] = continuation
        return message

    def failure_message(self) -> str:
        if self.status == "incomplete":
            reason = f" ({self.incomplete_reason})" if self.incomplete_reason else ""
            return f"{self.provider} Responses stream incomplete{reason}. No tool calls were executed."
        return self.error or f"{self.provider} Responses stream failed. No tool calls were executed."

    def emit_citation_suffix(self) -> list[Event]:
        """Append provider citations once, before the terminal ``Done``."""
        if self._citations_emitted:
            return []
        self._citations_emitted = True
        suffix = citation_markdown(self.citations, self.text)
        if not suffix:
            return []
        self.text += suffix
        return [ContentDelta(suffix)]

    def _note_search_event(self, event: Any, status: str) -> None:
        item_id = str(_attr(event, "item_id", "") or "")
        entry = {
            key: value
            for key, value in (
                ("item_id", item_id),
                ("status", status),
                ("action", _attr(event, "action", "")),
                ("queries", _attr(event, "queries", [])),
            )
            if value not in ("", None, [])
        }
        if item_id:
            for existing in self.web_search_calls:
                if existing.get("item_id") == item_id:
                    existing.update(entry)
                    return
            self._seen_search_ids.add(item_id)
        self.web_search_calls.append(entry)

    def _note_hosted_item(self, item: Any) -> None:
        item_type = str(_attr(item, "type", "") or "")
        if item_type == "message":
            self.citations.extend(citations_from_response_item(item))
            return
        if item_type != "web_search_call":
            return
        item_id = str(_attr(item, "id", "") or "")
        status = str(_attr(item, "status", "") or "in_progress")
        action = _attr(item, "action")
        entry: dict[str, Any] = {"item_id": item_id, "status": status}
        if action is not None:
            entry["action"] = str(_attr(action, "type", "") or "")
            queries = _attr(action, "queries")
            if isinstance(queries, list):
                entry["queries"] = [str(query) for query in queries]
        self._note_search_event(entry, status)
        wire_item = {"type": "web_search_call", "id": item_id, "status": status}
        if action is not None:
            wire_item["action"] = (
                dict(action) if isinstance(action, Mapping) else {
                    key: _attr(action, key)
                    for key in ("type", "query", "queries", "url")
                    if _attr(action, key, None) is not None
                }
            )
        if item_id and item_id not in {value.get("id") for value in self._search_items}:
            self._search_items.append(wire_item)
        elif item_id:
            for existing in self._search_items:
                if existing.get("id") == item_id:
                    existing.update(wire_item)

    def _note_reasoning_item(self, item: Any, index: int) -> None:
        """Record the provider's complete reasoning output item, in position.

        Only providers that require reasoning replay capture this. The item is
        stored verbatim — Aura never reconstructs it from the visible summary,
        because the summary is not the state the provider continues from.
        """
        if self.provider not in REASONING_CONTINUATION_PROVIDERS:
            return
        if _attr(item, "type") != "reasoning":
            return
        wire = reasoning_wire_item(item)
        if wire is not None:
            self._reasoning_items[index] = wire

    def _continuation_entries(self) -> list[dict[str, Any]]:
        """Return reasoning and completed function calls in output order."""
        if self.provider not in REASONING_CONTINUATION_PROVIDERS:
            return []
        ordered: list[tuple[int, dict[str, Any]]] = [
            (index, {"kind": "reasoning", "item": item})
            for index, item in self._reasoning_items.items()
        ]
        for index, slot in self._calls.items():
            if slot.get("complete") and slot.get("call_id") and slot.get("name"):
                ordered.append(
                    (index, {"kind": "function_call", "call_id": slot["call_id"]})
                )
        ordered.sort(key=lambda pair: pair[0])
        return [entry for _index, entry in ordered]

    def _event_index(self, event: Any, item: Any = None) -> int:
        raw = _attr(event, "output_index", None)
        if raw is not None:
            return int(raw or 0)
        item_id = str(_attr(event, "item_id", "") or _attr(item, "id", "") or "")
        if item_id in self._item_indexes:
            return self._item_indexes[item_id]
        return 0

    def _note_function_item(
        self,
        item: Any,
        index: int,
        *,
        added: bool,
        events: list[Event],
    ) -> None:
        if _attr(item, "type") != "function_call":
            return
        item_id = str(_attr(item, "id", "") or "")
        if item_id:
            self._item_indexes[item_id] = index
        slot = self._calls.setdefault(index, {"arguments": "", "ended": False})
        call_id = str(_attr(item, "call_id", "") or "")
        name = str(_attr(item, "name", "") or "")
        if call_id:
            slot["call_id"] = call_id
        if name:
            slot["name"] = name
        item_arguments = str(_attr(item, "arguments", "") or "")
        if item_arguments and not slot.get("arguments"):
            slot["arguments"] = item_arguments
        if added:
            if not slot.get("started") and slot.get("call_id") and slot.get("name"):
                slot["started"] = True
                events.append(
                    ToolCallStart(
                        index=index,
                        id=slot["call_id"],
                        name=slot["name"],
                    )
                )
            return
        slot["complete"] = True
        self._end_call(index, events)

    def _end_call(self, index: int, events: list[Event]) -> None:
        slot = self._calls.get(index)
        if not slot or slot.get("ended") or not slot.get("call_id"):
            return
        slot["ended"] = True
        if slot.get("started"):
            events.append(ToolCallEnd(index=index))

    def _finalize_completed(self, response: Any, events: list[Event]) -> None:
        self.status = "completed"
        self.response_id = str(_attr(response, "id", "") or self.response_id)
        self._record_usage(response, events)
        output = _attr(response, "output", None)
        if isinstance(output, list):
            for index, item in enumerate(output):
                item_type = _attr(item, "type")
                if item_type == "message":
                    self._note_hosted_item(item)
                    text = _message_output_text(item)
                    if text:
                        self.text = text
                elif item_type == "reasoning":
                    self._note_reasoning_item(item, index)
                    text = _reasoning_output_text(item)
                    if text:
                        self.reasoning = text
                elif item_type == "function_call":
                    self._note_function_item(item, index, added=True, events=events)
                    slot = self._calls[index]
                    slot["complete"] = True
                    self._end_call(index, events)
                elif item_type == "web_search_call":
                    self._note_hosted_item(item)
        self.finish_reason = "tool_calls" if self._completed_tool_calls() else "stop"

    def _set_incomplete(self, response: Any, events: list[Event]) -> None:
        self.status = "incomplete"
        self.response_id = str(_attr(response, "id", "") or self.response_id)
        details = _attr(response, "incomplete_details")
        self.incomplete_reason = str(_attr(details, "reason", "") or "") or None
        self._record_usage(response, events)

    def _set_failed(self, response: Any) -> None:
        self.status = "failed"
        self.response_id = str(_attr(response, "id", "") or self.response_id)
        error = _attr(response, "error")
        self.error_code = str(_attr(error, "code", "") or "") or None
        self.error = str(
            _attr(error, "message", "")
            or _attr(response, "message", "")
            or f"{self.provider} Responses stream failed"
        )

    def _record_usage(self, response: Any, events: list[Event]) -> None:
        if self._usage_emitted:
            return
        usage_event, usage_dict = _usage_to_events(response)
        if usage_event is None:
            return
        self._usage_emitted = True
        self.usage = usage_dict
        events.append(usage_event)

    def _completed_tool_calls(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            if not slot.get("complete") or not slot.get("call_id") or not slot.get("name"):
                continue
            arguments = str(slot.get("arguments", "") or "") or "{}"
            calls.append(
                {
                    "id": slot["call_id"],
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": arguments,
                    },
                }
            )
        return calls


def _message_output_text(item: Any) -> str:
    content = _attr(item, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(_attr(part, "text", "") or "")
        for part in content
        if _attr(part, "type", "output_text") in {"output_text", "text"}
    )


def _reasoning_output_text(item: Any) -> str:
    content = _attr(item, "summary", None)
    if not isinstance(content, list):
        content = _attr(item, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(str(_attr(part, "text", "") or "") for part in content)


__all__ = ["ResponsesProductionStreamParser"]
