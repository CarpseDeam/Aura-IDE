"""OpenAI-compatible Responses API streaming support for Aura.

This module owns the protocol translation between Aura's provider-neutral
tool capability names and the Responses API, plus normalization of Responses
stream events into Aura's existing event types.

Rules:
- Aura capability ``web_search`` maps to the built-in Responses tool
  ``{"type": "web_search"}`` (stable type; ``web_search_2025_08_26`` is a
  server-side alias and is never sent).
- Ordinary Aura function tools keep their shape as Responses function tools:
  ``{"type": "function", "name": ..., "description": ..., "parameters": ...}``.
- Raw DeepSeek/OpenAI response events are normalized here and never surface
  in conversation or UI code.
"""

from __future__ import annotations

from typing import Any

from aura.client.events import (
    ContentDelta,
    Event,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

# Stable Responses API built-in web search tool.
RESPONSES_WEB_SEARCH_TOOL: dict[str, Any] = {"type": "web_search"}

# Response event types handled by ResponsesStreamParser.
TERMINAL_RESPONSE_STATUSES: frozenset[str] = frozenset(
    {"completed", "incomplete", "failed"}
)


def translate_to_responses_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Translate Aura tool definitions into Responses API tools.

    - ``{"type": "web_search"}`` capabilities pass through as the built-in.
    - ``{"type": "function", "function": {...}}`` definitions are flattened
      into the Responses function shape: type/name/description/parameters.
    - Unknown shapes pass through unchanged.
    """
    if not tools:
        return None
    translated: list[dict[str, Any]] = []
    for tool in tools:
        tool_type = tool.get("type")
        if tool_type == "web_search":
            translated.append({"type": "web_search"})
            continue
        if tool_type == "function":
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            flat: dict[str, Any] = {"type": "function"}
            if fn.get("name"):
                flat["name"] = fn["name"]
            if fn.get("description"):
                flat["description"] = fn["description"]
            if fn.get("parameters"):
                flat["parameters"] = fn["parameters"]
            if "strict" in fn:
                flat["strict"] = fn["strict"]
            translated.append(flat)
            continue
        translated.append(tool)
    return translated


def build_native_web_search_request(
    question: str,
    context: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Return the exact ``client.responses.create(...)`` kwargs for web search.

    ``model`` may be None; the client layer resolves the provider default.
    """
    content = str(question or "").strip()
    if context and str(context).strip():
        content = f"{content}\n\nRelevant context:\n{str(context).strip()}"
    return {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "tools": [dict(RESPONSES_WEB_SEARCH_TOOL)],
        "tool_choice": "auto",
        "stream": True,
    }


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _dict_attr(obj: Any, name: str) -> dict[str, Any]:
    value = getattr(obj, name, None)
    return value if isinstance(value, dict) else {}


def _usage_to_events(response_obj: Any) -> tuple[Usage | None, dict[str, Any] | None]:
    """Extract final usage from a Responses ``response`` object."""
    usage = _attr(response_obj, "usage")
    if usage is None:
        return None, None
    input_tokens = int(_attr(usage, "input_tokens", 0) or 0)
    output_tokens = int(_attr(usage, "output_tokens", 0) or 0)
    total_tokens = int(_attr(usage, "total_tokens", 0) or 0)
    details = _dict_attr(usage, "input_tokens_details")
    cached = int(_attr(details, "cached_tokens", 0) or 0)
    if not cached:
        cached = int(_attr(usage, "prompt_cache_hit_tokens", 0) or 0)
    cache_miss = max(input_tokens - cached, 0)
    usage_dict = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "cache_hit_tokens": cached,
        "cache_miss_tokens": cache_miss,
        "total_tokens": total_tokens,
    }
    event = Usage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        cache_hit_tokens=cached,
        cache_miss_tokens=cache_miss,
    )
    return event, usage_dict


def _annotations_to_sources(item: Any) -> list[dict[str, Any]]:
    """Extract url_citation annotations from a message output item."""
    sources: list[dict[str, Any]] = []
    content = _attr(item, "content", None)
    if not isinstance(content, list):
        return sources
    for part in content:
        annotations = _attr(part, "annotations", None)
        if not isinstance(annotations, list):
            continue
        for annotation in annotations:
            if _attr(annotation, "type") != "url_citation":
                continue
            url = str(_attr(annotation, "url", "") or "").strip()
            if not url:
                continue
            sources.append(
                {
                    "title": str(_attr(annotation, "title", "") or "").strip(),
                    "url": url,
                }
            )
    return sources


class ResponsesStreamParser:
    """Normalize a Responses API SSE stream into Aura events.

    Usage::

        parser = ResponsesStreamParser()
        for chunk in stream:            # client.responses.create(..., stream=True)
            for event in parser.push(chunk):
                yield event
        payload = parser.finish()       # neutral research payload

    ``finish()`` never raises and never leaks raw response event types.
    """

    def __init__(self) -> None:
        self.text = ""
        self.status = "in_progress"
        self.sources: list[dict[str, Any]] = []
        self.web_search_calls: list[dict[str, Any]] = []
        self.usage: dict[str, Any] | None = None
        self.incomplete_reason: str | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.response_id: str = ""
        self.finish_reason: str | None = None
        self._seen_urls: set[str] = set()
        self._seen_search_calls: set[str] = set()
        self._pending_function_calls: dict[int, dict[str, Any]] = {}
        self._tool_call_ids: dict[int, str] = {}

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RESPONSE_STATUSES

    def cancel(self) -> None:
        """Mark the stream cancelled (caller observed cancel_event)."""
        self.status = "cancelled"

    def push(self, event: Any) -> list[Event]:
        """Consume one Responses stream event; return Aura events."""
        events: list[Event] = []
        event_type = _attr(event, "type", "")
        if event_type == "response.output_text.delta":
            delta = _attr(event, "delta", "") or ""
            if delta:
                self.text += delta
                events.append(ContentDelta(delta))
        elif event_type == "response.output_text.done":
            text = _attr(event, "text", "") or ""
            if text:
                self.text = text
        elif event_type == "response.output_item.added":
            item = _attr(event, "item")
            if item is not None:
                index = int(_attr(event, "output_index", 0) or 0)
                self._note_item(item, index, added=True, events=events)
        elif event_type == "response.output_item.done":
            item = _attr(event, "item")
            if item is not None:
                index = int(_attr(event, "output_index", 0) or 0)
                self._note_item(item, index, added=False, events=events)
        elif event_type == "response.web_search_call.in_progress":
            self._note_search_call(event, "in_progress")
        elif event_type == "response.web_search_call.searching":
            self._note_search_call(event, "searching")
        elif event_type == "response.web_search_call.completed":
            self._note_search_call(event, "completed")
        elif event_type == "response.function_call_arguments.delta":
            index = int(_attr(event, "output_index", 0) or 0)
            delta = _attr(event, "delta", "") or ""
            slot = self._pending_function_calls.setdefault(index, {"arguments": ""})
            slot["arguments"] += delta
            events.append(ToolCallArgsDelta(index=index, args_chunk=delta))
        elif event_type == "response.function_call_arguments.done":
            index = int(_attr(event, "output_index", 0) or 0)
            arguments = _attr(event, "arguments", "") or ""
            slot = self._pending_function_calls.setdefault(index, {"arguments": ""})
            slot["arguments"] = arguments
        elif event_type == "response.completed":
            response_obj = _attr(event, "response")
            if response_obj is not None:
                self._finalize_response(response_obj, events)
        elif event_type == "response.incomplete":
            response_obj = _attr(event, "response")
            if response_obj is not None:
                self.status = "incomplete"
                details = _dict_attr(response_obj, "incomplete_details")
                self.incomplete_reason = str(
                    details.get("reason") or _attr(response_obj, "reason", "") or ""
                ) or None
                usage_event, usage_dict = _usage_to_events(response_obj)
                if usage_event is not None:
                    self.usage = usage_dict
                    events.append(usage_event)
                self._collect_output_sources(response_obj)
                self.response_id = str(_attr(response_obj, "id", "") or "")
        elif event_type == "response.failed":
            response_obj = _attr(event, "response")
            if response_obj is not None:
                self.status = "failed"
                error = _dict_attr(response_obj, "error")
                self.error_code = str(error.get("code") or "") or None
                self.error = str(
                    error.get("message")
                    or _attr(response_obj, "message", "")
                    or "Responses API request failed"
                )
                self.response_id = str(_attr(response_obj, "id", "") or "")
        elif event_type == "error":
            self.status = "failed"
            self.error_code = str(_attr(event, "code", "") or "") or None
            self.error = str(_attr(event, "message", "") or "Responses API stream error")
        return events

    # -- internals --------------------------------------------------------

    def _note_item(self, item: Any, index: int, *, added: bool, events: list[Event]) -> None:
        item_type = _attr(item, "type")
        if item_type == "message":
            for source in _annotations_to_sources(item):
                self._add_source(source)
        elif item_type == "web_search_call":
            item_id = str(_attr(item, "id", "") or "")
            status = str(_attr(item, "status", "") or "in_progress")
            if item_id and item_id not in self._seen_search_calls:
                self._seen_search_calls.add(item_id)
                self.web_search_calls.append(
                    {
                        "item_id": item_id,
                        "status": status,
                        "search_recipient": str(_attr(item, "search_recipient", "") or ""),
                    }
                )
        elif item_type == "function_call":
            call_id = str(_attr(item, "id", "") or "")
            name = str(_attr(item, "name", "") or "")
            self._tool_call_ids[index] = call_id
            if added:
                events.append(ToolCallStart(index=index, id=call_id, name=name))
            else:
                slot = self._pending_function_calls.setdefault(index, {"arguments": ""})
                if not slot["arguments"]:
                    slot["arguments"] = str(_attr(item, "arguments", "") or "")
                events.append(ToolCallEnd(index=index))

    def _note_search_call(self, event: Any, status: str) -> None:
        item_id = str(_attr(event, "item_id", "") or "")
        if item_id and item_id not in self._seen_search_calls:
            self._seen_search_calls.add(item_id)
        entry = {
            "item_id": item_id,
            "status": status,
            "search_recipient": str(
                _attr(event, "search_recipient", "")
                or _attr(event, "search_context", None)
                or ""
            ),
        }
        if item_id:
            for existing in self.web_search_calls:
                if existing.get("item_id") == item_id:
                    existing["status"] = status
                    return
        self.web_search_calls.append(entry)

    def _add_source(self, source: dict[str, Any]) -> None:
        url = source.get("url")
        if not url or url in self._seen_urls:
            return
        self._seen_urls.add(url)
        self.sources.append(source)

    def _collect_output_sources(self, response_obj: Any) -> None:
        output = _attr(response_obj, "output", None)
        if not isinstance(output, list):
            return
        for item in output:
            item_type = _attr(item, "type")
            if item_type == "message":
                for source in _annotations_to_sources(item):
                    self._add_source(source)
            elif item_type == "web_search_call":
                item_id = str(_attr(item, "id", "") or "")
                if item_id and item_id not in self._seen_search_calls:
                    self._seen_search_calls.add(item_id)
                    self.web_search_calls.append(
                        {
                            "item_id": item_id,
                            "status": str(_attr(item, "status", "") or "in_progress"),
                            "search_recipient": str(
                                _attr(item, "search_recipient", "") or ""
                            ),
                        }
                    )

    def _finalize_response(self, response_obj: Any, events: list[Event]) -> None:
        self.status = "completed"
        usage_event, usage_dict = _usage_to_events(response_obj)
        if usage_event is not None:
            self.usage = usage_dict
            events.append(usage_event)
        self._collect_output_sources(response_obj)
        self.response_id = str(_attr(response_obj, "id", "") or "")
        self.finish_reason = str(_attr(response_obj, "status", "completed") or "completed")

    def finish(self) -> dict[str, Any]:
        """Return the neutral final research payload for this stream."""
        return {
            "status": self.status,
            "text": self.text,
            "sources": [dict(s) for s in self.sources],
            "web_search_calls": [dict(c) for c in self.web_search_calls],
            "usage": dict(self.usage) if self.usage else None,
            "incomplete_reason": self.incomplete_reason,
            "error": self.error,
            "error_code": self.error_code,
            "response_id": self.response_id,
        }
