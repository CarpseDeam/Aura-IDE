"""DeepSeek V4 Responses projection, request construction, and parsing."""

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
from aura.client.responses_common import (
    TERMINAL_RESPONSE_STATUSES,
    TERMINAL_STREAM_STATUSES,
    translate_to_responses_tools,
)
from aura.client.responses_common import (
    attr as _attr,
)
from aura.client.responses_common import (
    usage_to_events as _usage_to_events,
)
from aura.providers.base import ThinkingMode, normalize_thinking_mode


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
    """Project canonical content without local/provider metadata."""
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
                parts.append({"type": "input_image", "image_url": url})
            continue
        if part_type == "input_image" and isinstance(part.get("image_url"), str):
            parts.append({"type": "input_image", "image_url": part["image_url"]})
    return parts


def project_deepseek_responses_input(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Project canonical Aura messages onto stateless Responses input items.

    Fresh dictionaries are constructed, so canonical history is never rewritten.
    Reasoning fields, provider output-item ids, signatures, and Aura-local
    bookkeeping are intentionally not represented on the wire.
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
    """Build one stateless DeepSeek V4 Responses request."""
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


class DeepSeekResponsesStreamParser:
    """Normalize one DeepSeek production Responses stream.

    A function call is exposed only after its argument stream is complete;
    terminal incomplete/failed responses never produce an executable ``Done``.
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
