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

from collections.abc import Mapping
from typing import Any

from aura.client.events import (
    ContentDelta,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from aura.providers.base import ThinkingMode, normalize_thinking_mode

# Stable Responses API built-in web search tool.
RESPONSES_WEB_SEARCH_TOOL: dict[str, Any] = {"type": "web_search"}

# Forces the model to run the built-in search instead of answering directly.
RESPONSES_WEB_SEARCH_TOOL_CHOICE: dict[str, Any] = {"type": "web_search"}

# Response statuses that end the server-side response.
TERMINAL_RESPONSE_STATUSES: frozenset[str] = frozenset(
    {"completed", "incomplete", "failed"}
)

# Statuses that make a consumed stream terminal, including local cancellation.
# ``in_progress`` is never one of these: a stream that stops without reaching a
# terminal status is reported as ``failed``.
TERMINAL_STREAM_STATUSES: frozenset[str] = TERMINAL_RESPONSE_STATUSES | {"cancelled"}


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
            if fn.get("parameters") is not None:
                # An explicit empty schema is meaningful for zero-argument
                # tools; only a missing key is dropped.
                flat["parameters"] = fn["parameters"]
            if "strict" in fn:
                flat["strict"] = fn["strict"]
            translated.append(flat)
            continue
        translated.append(tool)
    return translated


def _visible_text(value: Any) -> str:
    """Return visible text from a canonical message content value."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in value
        if isinstance(part, Mapping)
        and part.get("type") in {"text", "input_text", "output_text"}
        and isinstance(part.get("text", ""), str)
    )


def _responses_content(value: Any, *, role: str) -> str | list[dict[str, Any]]:
    """Project canonical content without carrying local/provider metadata."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""

    parts: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, Mapping):
            continue
        part_type = part.get("type")
        text = part.get("text")
        if part_type in {"text", "input_text"} and isinstance(text, str):
            parts.append({"type": "input_text", "text": text})
            continue
        if role == "assistant" and part_type == "output_text" and isinstance(text, str):
            parts.append({"type": "output_text", "text": text})
            continue
        if part_type == "image_url":
            image_url = part.get("image_url")
            if isinstance(image_url, Mapping):
                url = image_url.get("url")
            else:
                url = image_url
            if isinstance(url, str) and url:
                # DeepSeek accepts the Responses input_image shape but documents
                # that images are replaced by a placeholder. Keep the mapping
                # protocol-native and discard Chat-only detail/metadata fields.
                parts.append({"type": "input_image", "image_url": url})
            continue
        if part_type == "input_image" and isinstance(part.get("image_url"), str):
            parts.append({"type": "input_image", "image_url": part["image_url"]})
    return parts


def project_deepseek_responses_input(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Project canonical Aura messages onto stateless Responses input items.

    This is the sole owner of the DeepSeek production request projection. It
    constructs fresh dictionaries, so the canonical transcript is never
    rewritten. Reasoning fields, provider output-item ids, signatures, and
    Aura-local bookkeeping are intentionally not represented on the wire.
    """
    instructions: str | None = None
    input_items: list[dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "system":
            text = _visible_text(message.get("content", ""))
            if text:
                instructions = text if instructions is None else f"{instructions}\n\n{text}"
            continue

        if role in {"user", "assistant"}:
            content = _responses_content(message.get("content", ""), role=role)
            if role == "user" or _visible_text(message.get("content", "")):
                input_items.append({"role": role, "content": content})

            if role == "assistant":
                tool_calls = message.get("tool_calls")
                if not isinstance(tool_calls, list):
                    continue
                for call in tool_calls:
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    call_id = call.get("id")
                    name = function.get("name")
                    arguments = function.get("arguments", "")
                    if not isinstance(call_id, str) or not call_id:
                        continue
                    if not isinstance(name, str) or not name:
                        continue
                    if not isinstance(arguments, str):
                        arguments = str(arguments)
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        }
                    )
            continue

        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            output = message.get("content", "")
            if not isinstance(output, str):
                output = str(output)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )

    return instructions, input_items


def build_deepseek_responses_request(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str,
    thinking: ThinkingMode,
    temperature: float = 0.7,
) -> dict[str, Any]:
    """Build one stateless DeepSeek V4 Responses request.

    The input prefix is derived in canonical order and contains no response
    chaining fields. ``tool_choice`` is deliberately absent for ordinary
    production turns; the native web-search path owns its explicit choice.
    """
    instructions, input_items = project_deepseek_responses_input(messages)
    mode = normalize_thinking_mode(thinking) or "high"
    request: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "reasoning": {"effort": mode},
        "stream": True,
    }
    if instructions is not None:
        request["instructions"] = instructions
    translated_tools = translate_to_responses_tools(tools)
    if translated_tools:
        request["tools"] = translated_tools
    if mode == "off":
        request["temperature"] = temperature
    return request


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
        "tool_choice": dict(RESPONSES_WEB_SEARCH_TOOL_CHOICE),
        "stream": True,
    }


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read one field from either a mapping or an SDK model object.

    Responses payloads reach us as parsed OpenAI SDK objects in production and
    as plain dictionaries from raw SSE decoding, stubs, and providers that
    return loosely-typed extras.  Every nested read (usage details, incomplete
    details, error, citation annotations) goes through here so both shapes
    parse identically.
    """
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
        return default if value is None else value
    value = getattr(obj, name, default)
    return default if value is None else value


def _usage_to_events(response_obj: Any) -> tuple[Usage | None, dict[str, Any] | None]:
    """Extract final usage from a Responses ``response`` object."""
    usage = _attr(response_obj, "usage")
    if usage is None:
        return None, None
    input_tokens = int(_attr(usage, "input_tokens", 0) or 0)
    output_tokens = int(_attr(usage, "output_tokens", 0) or 0)
    total_tokens = int(_attr(usage, "total_tokens", 0) or 0)
    details = _attr(usage, "input_tokens_details")
    cached = int(_attr(details, "cached_tokens", 0) or 0)
    if not cached:
        cached = int(_attr(usage, "prompt_cache_hit_tokens", 0) or 0)
    cache_miss = int(_attr(usage, "prompt_cache_miss_tokens", 0) or 0)
    if not cache_miss:
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
            url = _clean_source_url(_attr(annotation, "url", ""))
            if not url:
                continue
            sources.append(
                {
                    "title": str(_attr(annotation, "title", "") or "").strip(),
                    "url": url,
                }
            )
    return sources


def _clean_source_url(raw: Any) -> str:
    """Normalize a source URL, dropping the provider's call-id fragment.

    Search-call URLs come back as ``https://host/path/#ws_call_id=call_01_...``;
    the fragment is bookkeeping, not part of the page the model read.
    """
    url = str(raw or "").strip()
    if not url:
        return ""
    fragment = url.find("#ws_call_id=")
    if fragment != -1:
        url = url[:fragment]
    return url.rstrip("#")


def _search_call_action(item: Any) -> dict[str, Any]:
    """Normalize the ``action`` of a web_search_call item.

    DeepSeek reports what the search actually did here — ``search`` with the
    issued queries, or ``open_page`` with the URL it read.  This is where the
    real evidence lives: the provider emits no url_citation annotations.
    """
    action = _attr(item, "action")
    if action is None:
        return {}
    queries = _attr(action, "queries", None)
    normalized: dict[str, Any] = {
        "action": str(_attr(action, "type", "") or "").strip(),
        "url": _clean_source_url(_attr(action, "url", "")),
        "queries": [
            str(q).strip()
            for q in queries
            if str(q).strip() and not str(q).startswith("ws_call_id=")
        ]
        if isinstance(queries, list)
        else [],
    }
    return normalized


def _source_from_search_call(item: Any, status: str) -> dict[str, Any] | None:
    """Return the page a completed search call read, as a source."""
    if status != "completed":
        return None
    action = _search_call_action(item)
    url = action.get("url") or ""
    if not url:
        return None
    host = ""
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0]
    return {"title": host or url, "url": url}


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
        """True once the server reported a terminal response status."""
        return self.status in TERMINAL_RESPONSE_STATUSES

    @property
    def settled(self) -> bool:
        """True once this stream has an honest terminal status of any kind."""
        return self.status in TERMINAL_STREAM_STATUSES

    def cancel(self) -> None:
        """Mark the stream cancelled (caller observed cancel_event)."""
        self.status = "cancelled"

    def fail(self, message: str, code: str | None = None) -> None:
        """Mark the stream failed, keeping any partial text and sources.

        Used when the stream stops without a terminal response event (first-
        event timeout, inter-event timeout, or an early close).  A consumed
        stream must never be reported as still ``in_progress``.
        """
        self.status = "failed"
        self.error = self.error or message
        self.error_code = self.error_code or code

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
                details = _attr(response_obj, "incomplete_details")
                self.incomplete_reason = str(
                    _attr(details, "reason", "")
                    or _attr(response_obj, "reason", "")
                    or ""
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
                error = _attr(response_obj, "error")
                self.error_code = str(_attr(error, "code", "") or "") or None
                self.error = str(
                    _attr(error, "message", "")
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
            self._record_search_call_item(item)
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

    def _record_search_call_item(self, item: Any) -> None:
        """Record one web_search_call output item and any page it read."""
        item_id = str(_attr(item, "id", "") or "")
        status = str(_attr(item, "status", "") or "in_progress")
        entry = {
            "item_id": item_id,
            "status": status,
            "search_recipient": str(_attr(item, "search_recipient", "") or ""),
            **_search_call_action(item),
        }
        if item_id and item_id in self._seen_search_calls:
            for existing in self.web_search_calls:
                if existing.get("item_id") == item_id:
                    existing.update(entry)
                    break
        else:
            if item_id:
                self._seen_search_calls.add(item_id)
            self.web_search_calls.append(entry)

        source = _source_from_search_call(item, status)
        if source is not None:
            self._add_source(source)

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
                self._record_search_call_item(item)

    def _final_message_text(self, response_obj: Any) -> str:
        """Text of the last assistant message in the completed output.

        The provider emits interim ``commentary`` messages before the final
        answer, so the last non-empty message wins.
        """
        output = _attr(response_obj, "output", None)
        if not isinstance(output, list):
            return ""
        final = ""
        for item in output:
            if _attr(item, "type") != "message":
                continue
            content = _attr(item, "content", None)
            if not isinstance(content, list):
                continue
            text = "".join(
                str(_attr(part, "text", "") or "")
                for part in content
                if _attr(part, "type", "output_text") == "output_text"
            ).strip()
            if text:
                final = text
        return final

    def _finalize_response(self, response_obj: Any, events: list[Event]) -> None:
        self.status = "completed"
        usage_event, usage_dict = _usage_to_events(response_obj)
        if usage_event is not None:
            self.usage = usage_dict
            events.append(usage_event)
        # Prefer the completed response's own final message over whatever the
        # deltas happened to leave behind.
        final_text = self._final_message_text(response_obj)
        if final_text:
            self.text = final_text
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


class DeepSeekResponsesStreamParser:
    """Normalize one DeepSeek production Responses stream.

    The parser owns the translation back to Aura's existing chat-shaped
    assistant message. It only exposes a function call after its argument
    stream is complete; terminal incomplete/failed responses never produce a
    ``Done`` message for the conversation loop to execute.
    """

    def __init__(self) -> None:
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
        elif event_type == "response.reasoning_text.delta":
            delta = str(_attr(event, "delta", "") or "")
            if delta:
                self.reasoning += delta
                events.append(ReasoningDelta(delta))
        elif event_type == "response.reasoning_text.done":
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
                self._note_function_item(
                    item,
                    self._event_index(event, item),
                    added=True,
                    events=events,
                )
        elif event_type == "response.output_item.done":
            item = _attr(event, "item")
            if item is not None:
                self._note_function_item(
                    item,
                    self._event_index(event, item),
                    added=False,
                    events=events,
                )
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
        return message

    def failure_message(self) -> str:
        if self.status == "incomplete":
            reason = f" ({self.incomplete_reason})" if self.incomplete_reason else ""
            return f"DeepSeek Responses stream incomplete{reason}. No tool calls were executed."
        return self.error or "DeepSeek Responses stream failed. No tool calls were executed."

    # -- internals --------------------------------------------------------

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
                    text = _message_output_text(item)
                    if text:
                        self.text = text
                elif item_type == "reasoning":
                    text = _reasoning_output_text(item)
                    if text:
                        self.reasoning = text
                elif item_type == "function_call":
                    self._note_function_item(item, index, added=True, events=events)
                    slot = self._calls[index]
                    slot["complete"] = True
                    self._end_call(index, events)
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
            or "DeepSeek Responses stream failed"
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
    content = _attr(item, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(str(_attr(part, "text", "") or "") for part in content)
