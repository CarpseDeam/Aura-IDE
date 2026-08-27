"""Provider-owned Responses reasoning state carried across Aura tool rounds.

Aura sends stateless full-history Responses requests rather than
``previous_response_id``.  OpenAI's reasoning/function-calling guidance
requires the reasoning output items of a response to be replayed together with
their ``function_call`` and ``function_call_output`` items so a reasoning model
can continue the same chain on the next request.

This module owns that state end to end:

* it captures the complete, JSON-safe reasoning output item the provider
  returned — including ``encrypted_content`` when the provider returns it;
* it stores the item as Aura-local, provider-tagged assistant metadata, so
  persistence keeps it verbatim and chat rendering never sees it; and
* it hands the request projection an ordered replay plan.

The metadata is deliberately *not* rendered anywhere: Aura keeps exposing only
the provider-supplied visible reasoning summary through ``reasoning_content``.
Nothing here invents or exposes raw chain-of-thought.

Only providers listed in :data:`REASONING_CONTINUATION_PROVIDERS` capture or
replay this state.  DeepSeek's Responses policy intentionally omits prior
reasoning from every continuation and is unchanged.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

#: Aura-local assistant metadata key holding provider reasoning wire state.
#: Local-only: every wire projection either builds fresh items (Responses,
#: Anthropic, Google) or strips the key explicitly (chat completions).
AURA_PROVIDER_REASONING_KEY = "aura_provider_reasoning"

#: Providers whose Responses reasoning items Aura replays. OpenAI requires it
#: for stateless reasoning continuation; DeepSeek deliberately does not.
REASONING_CONTINUATION_PROVIDERS: frozenset[str] = frozenset({"openai"})

#: The fields of an OpenAI Responses reasoning output item that a stateless
#: continuation needs. ``encrypted_content`` carries the provider-owned state
#: and is stored verbatim when returned; ``summary`` is the provider's own
#: visible summary, which Aura already surfaces separately.
_REASONING_ITEM_FIELDS: tuple[str, ...] = (
    "id",
    "type",
    "summary",
    "content",
    "encrypted_content",
    "status",
)


def json_safe(value: Any) -> Any:
    """Return a JSON-serializable copy of an SDK model, mapping, or scalar."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return json_safe(dump(exclude_none=True))
        except Exception:  # noqa: BLE001 - fall through to attribute reading
            pass
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {
            str(key): json_safe(item)
            for key, item in data.items()
            if not str(key).startswith("_")
        }
    return str(value)


def reasoning_wire_item(item: Any) -> dict[str, Any] | None:
    """Return the JSON-safe reasoning output item, or ``None`` if it is not one.

    Only the fields a stateless continuation replays are kept, so nothing
    unrelated from an SDK object is echoed back onto the wire.
    """
    payload = json_safe(item)
    if not isinstance(payload, dict) or payload.get("type") != "reasoning":
        return None
    wire = {
        key: payload[key]
        for key in _REASONING_ITEM_FIELDS
        if key in payload and payload[key] not in (None, "", [])
    }
    wire["type"] = "reasoning"
    return wire


def reasoning_continuation_metadata(
    *,
    provider: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return provider-tagged continuation metadata, or ``None``.

    Metadata is produced only when the response actually needs continuation:
    at least one reasoning item *and* at least one completed function call.
    A turn that returned no client tool call has nothing to continue, so no
    provider reasoning item is stored for it.
    """
    if provider not in REASONING_CONTINUATION_PROVIDERS:
        return None
    kinds = {entry.get("kind") for entry in entries}
    if not {"reasoning", "function_call"} <= kinds:
        return None
    return {
        "provider": provider,
        "entries": copy.deepcopy(entries),
    }


def continuation_entries(
    message: Mapping[str, Any],
    *,
    provider: str,
) -> list[dict[str, Any]]:
    """Return the ordered replay plan stored on *message* for *provider*.

    Returns an empty list for any other provider, so OpenAI reasoning state
    never reaches DeepSeek, Anthropic, Gemini, OpenRouter, or an external CLI
    after a conversation's provider is switched.
    """
    if provider not in REASONING_CONTINUATION_PROVIDERS:
        return []
    metadata = message.get(AURA_PROVIDER_REASONING_KEY)
    if not isinstance(metadata, Mapping) or metadata.get("provider") != provider:
        return []
    entries = metadata.get("entries")
    if not isinstance(entries, list):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


__all__ = [
    "AURA_PROVIDER_REASONING_KEY",
    "REASONING_CONTINUATION_PROVIDERS",
    "continuation_entries",
    "json_safe",
    "reasoning_continuation_metadata",
    "reasoning_wire_item",
]
