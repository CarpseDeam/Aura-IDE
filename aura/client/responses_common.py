"""Provider-neutral helpers shared by Aura Responses protocols."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aura.client.events import Usage

# Response statuses that end the server-side response.
TERMINAL_RESPONSE_STATUSES: frozenset[str] = frozenset(
    {"completed", "incomplete", "failed"}
)

# Statuses that make a consumed stream terminal, including local cancellation.
TERMINAL_STREAM_STATUSES: frozenset[str] = TERMINAL_RESPONSE_STATUSES | {"cancelled"}


def translate_to_responses_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Translate Aura tool definitions into Responses API tools.

    Chat-shaped function definitions are flattened into the Responses function
    shape. Unknown shapes pass through unchanged.
    """
    if not tools:
        return None
    translated: list[dict[str, Any]] = []
    for tool in tools:
        tool_type = tool.get("type")
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


def attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a mapping or an SDK model object."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
        return default if value is None else value
    value = getattr(obj, name, default)
    return default if value is None else value


def usage_to_events(response_obj: Any) -> tuple[Usage | None, dict[str, Any] | None]:
    """Extract final usage from a Responses ``response`` object."""
    usage = attr(response_obj, "usage")
    if usage is None:
        return None, None
    input_tokens = int(attr(usage, "input_tokens", 0) or 0)
    output_tokens = int(attr(usage, "output_tokens", 0) or 0)
    total_tokens = int(attr(usage, "total_tokens", 0) or 0)
    details = attr(usage, "input_tokens_details")
    cached = int(attr(details, "cached_tokens", 0) or 0)
    if not cached:
        cached = int(attr(usage, "prompt_cache_hit_tokens", 0) or 0)
    cache_miss = int(attr(usage, "prompt_cache_miss_tokens", 0) or 0)
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
