"""Native Responses web-search request and protocol parsing."""

from __future__ import annotations

from typing import Any

from aura.client.events import (
    ContentDelta,
    Event,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
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

# Stable Responses API built-in web search tool.
RESPONSES_WEB_SEARCH_TOOL: dict[str, Any] = {"type": "web_search"}

# Forces the model to run the built-in search instead of answering directly.
RESPONSES_WEB_SEARCH_TOOL_CHOICE: dict[str, Any] = {"type": "web_search"}


def build_native_web_search_request(
    question: str,
    context: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Return the exact ``client.responses.create(...)`` kwargs for web search."""
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
    """Normalize a source URL, dropping the provider's call-id fragment."""
    url = str(raw or "").strip()
    if not url:
        return ""
    fragment = url.find("#ws_call_id=")
    if fragment != -1:
        url = url[:fragment]
    return url.rstrip("#")


def _search_call_action(item: Any) -> dict[str, Any]:
    """Normalize the ``action`` of a web_search_call item."""
    action = _attr(item, "action")
    if action is None:
        return {}
    queries = _attr(action, "queries", None)
    return {
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
    """Normalize one native Responses web-search stream into Aura events."""

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

    @property
    def settled(self) -> bool:
        return self.status in TERMINAL_STREAM_STATUSES

    def cancel(self) -> None:
        self.status = "cancelled"

    def fail(self, message: str, code: str | None = None) -> None:
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
