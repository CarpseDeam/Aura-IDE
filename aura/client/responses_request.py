"""Canonical-history projection and request construction for Responses turns.

This module owns one responsibility: turning Aura's canonical message log into
one stateless Responses API request.  Stream consumption and completed-message
construction belong to :mod:`aura.client.responses_stream`; dispatch and
lifecycle belong to :mod:`aura.client.responses_transport`.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from aura.client.hosted_search import AURA_HOSTED_SEARCH_KEY
from aura.client.reasoning import resolve_responses_reasoning
from aura.client.responses_common import translate_to_responses_tools
from aura.client.responses_continuation import continuation_entries
from aura.providers.base import ThinkingMode


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


def _wire_function_calls(value: Any) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(call_id, function_call item)`` pairs in canonical order."""
    if not isinstance(value, list):
        return []
    calls: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for call in value:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            continue
        call_id = call.get("id")
        name = function.get("name")
        arguments = function.get("arguments", "")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            continue
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(arguments, str):
            arguments = str(arguments)
        seen.add(call_id)
        calls.append(
            (
                call_id,
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                },
            )
        )
    return calls


def _assistant_wire_items(
    entries: list[dict[str, Any]],
    calls: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return the assistant's reasoning/function-call items in wire order.

    With no stored continuation plan this is exactly the canonical tool-call
    order.  With one, the provider's own reasoning items are replayed in their
    original positions, so a reasoning item always precedes the function call
    it produced and stateless continuation stays correct.
    """
    items: list[dict[str, Any]] = []
    by_id = dict(calls)
    emitted: set[str] = set()
    for entry in entries:
        kind = entry.get("kind")
        if kind == "reasoning":
            item = entry.get("item")
            if isinstance(item, Mapping) and item.get("type") == "reasoning":
                items.append(copy.deepcopy(dict(item)))
            continue
        if kind != "function_call":
            continue
        call_id = entry.get("call_id")
        if isinstance(call_id, str) and call_id in by_id and call_id not in emitted:
            emitted.add(call_id)
            items.append(by_id[call_id])
    for call_id, item in calls:
        if call_id not in emitted:
            items.append(item)
    return items


def project_responses_input(
    messages: list[dict[str, Any]],
    *,
    provider: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Project canonical Aura messages onto stateless Responses input items.

    Fresh dictionaries are constructed, so canonical history is never rewritten.
    Foreign-provider metadata, signatures, visible ``reasoning_content``, and
    Aura-local bookkeeping are intentionally not represented on the wire.
    Matching Responses hosted-search call items and provider reasoning items
    are replayed only to the provider that produced them, preserving same-turn
    server context without contaminating a later request after a provider
    switch.
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
            if role == "assistant":
                hosted = message.get(AURA_HOSTED_SEARCH_KEY)
                if (
                    isinstance(hosted, Mapping)
                    and hosted.get("provider") == provider
                    and isinstance(hosted.get("wire_items"), list)
                ):
                    input_items.extend(
                        dict(item)
                        for item in hosted["wire_items"]
                        if isinstance(item, Mapping)
                    )
            if role == "user" or _visible_text(message.get("content", "")):
                input_items.append({"role": role, "content": content})

            if role == "assistant":
                input_items.extend(
                    _assistant_wire_items(
                        continuation_entries(message, provider=provider),
                        _wire_function_calls(message.get("tool_calls")),
                    )
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


def build_responses_request(
    *,
    provider: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    hosted_tools: list[dict[str, Any]] | None,
    model: str,
    thinking: ThinkingMode,
    temperature: float = 0.7,
) -> dict[str, Any]:
    """Build one stateless production Responses request."""
    instructions, input_items = project_responses_input(messages, provider=provider)
    resolved = resolve_responses_reasoning(
        provider=provider, model=model, thinking=thinking
    )
    request: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": True,
    }
    if resolved.reasoning is not None:
        request["reasoning"] = dict(resolved.reasoning)
    if instructions is not None:
        request["instructions"] = instructions
    translated_tools = translate_to_responses_tools(tools) or []
    translated_tools.extend(dict(tool) for tool in (hosted_tools or []))
    if translated_tools:
        request["tools"] = translated_tools
    if resolved.send_temperature:
        request["temperature"] = temperature
    return request


__all__ = ["build_responses_request", "project_responses_input"]
