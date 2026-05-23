import json
from typing import Any

from aura.client.events import (
    ContentDelta,
    Event,
    ToolCallEnd,
    ToolCallStart,
)
from aura.providers.google_cloud.signatures import decode_signature, make_message_json_safe


def _lookup_thought_signature(
    google_call_metadata: dict[str, dict[str, Any]] | None,
    call_id: str,
    name: str = "",
) -> dict | None:
    """Look up thought_signature metadata by call_id, falling back to fn:name."""
    if not google_call_metadata:
        return None
    metadata = google_call_metadata.get(call_id)
    if metadata is None and name:
        metadata = google_call_metadata.get(f"fn:{name}")
    return metadata


def aura_tools_to_google_declarations(tools: list[dict]) -> list[dict]:
    """Convert Aura (OpenAI-compatible) tools to Google function declarations.

    Each Aura tool is ``{"type": "function", "function": {...}}``.
    We extract the inner ``function`` dict, keeping name, description,
    and parameters.
    """
    declarations: list[dict] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        declarations.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object"}),
            }
        )
    return declarations


def aura_messages_to_google_contents(
    messages: list[dict],
    google_call_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[str | None, list[dict]]:
    """Convert Aura (OpenAI-format) messages to Google's system_instruction + contents.

    Returns ``(system_instruction, contents)``.
    """
    system_parts: list[str] = []
    contents: list[dict] = []
    current_tool_parts: list[dict] = []

    def _flush_tools() -> None:
        if current_tool_parts:
            contents.append({
                "role": "user",
                "parts": list(current_tool_parts),
            })
            current_tool_parts.clear()

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            tool_name = msg.get("name", tool_call_id)
            content = msg.get("content", "")
            
            try:
                response_dict = json.loads(content)
                if not isinstance(response_dict, dict):
                    response_dict = {"result": response_dict}
            except Exception:
                response_dict = {"result": str(content)}

            current_tool_parts.append(
                {
                    "function_response": {
                        "id": tool_call_id,
                        "name": tool_name,
                        "response": response_dict,
                    }
                }
            )
            continue

        if role == "user":
            _flush_tools()
            parts: list[dict] = []
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    parts.append({"text": content})
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        text = part.get("text")
                        if text:
                            parts.append({"text": text})
                    # image_url parts are skipped — Google has different image handling
            if not parts:
                parts.append({"text": ""})
            contents.append({"role": "user", "parts": parts})
            continue

        if role == "assistant":
            _flush_tools()
            parts: list[dict] = []

            # Reasoning content → text
            rc = msg.get("reasoning_content")
            if rc and isinstance(rc, str):
                parts.append({"text": rc})

            # Content
            content = msg.get("content")
            if isinstance(content, str) and content:
                parts.append({"text": content})

            # Tool calls → function_call parts
            tool_calls = msg.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    if not isinstance(fn, dict):
                        continue
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    call_id = str(tc.get("id", ""))
                    part = {
                        "function_call": {
                            "id": call_id,
                            "name": str(fn.get("name", "")),
                            "args": args,
                        }
                    }
                    metadata = _lookup_thought_signature(
                        google_call_metadata, call_id, str(fn.get("name", ""))
                    )
                    if metadata:
                        sig = metadata.get("thought_signature")
                        if sig:
                            part["thought_signature"] = (
                                decode_signature(sig) if isinstance(sig, str) else sig
                            )
                    parts.append(part)

            if not parts:
                parts.append({"text": ""})
            contents.append({"role": "model", "parts": parts})
            continue

    _flush_tools()
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def google_response_to_events(
    response_chunks: Any,
    content_buf: list[str],
    reasoning_buf: list[str],
    tool_calls: dict[int, dict[str, Any]],
    seen_tool_starts: set[int],
) -> Any:
    """Generator that processes Google GenAI streaming response chunks.

    Yields Aura Event objects.  The caller should drive this by iterating
    over the streaming response and calling ``yield from`` on the result
    of passing each chunk through this function.

    This is designed to be called as a regular function whose result is
    yielded from; the caller maintains the buffers and state.  However,
    since the chunk-by-chunk nature of streaming makes this awkward, we
    expose a helper that callers iterate inside their main loop.
    """
    # We return a list of events for the caller to yield from.
    events: list[Event] = []

    if not hasattr(response_chunks, "candidates") or not response_chunks.candidates:
        return events

    candidate = response_chunks.candidates[0]

    content = getattr(candidate, "content", None)
    if content is None or not hasattr(content, "parts") or content.parts is None:
        return events

    for part in content.parts:
        if hasattr(part, "text") and part.text:
            text = part.text
            content_buf.append(text)
            events.append(ContentDelta(text))
        elif hasattr(part, "function_call") and part.function_call is not None:
            fc = part.function_call
            idx = len(tool_calls)
            tc = {
                "id": getattr(fc, "id", f"call_{idx}"),
                "type": "function",
                "function": {
                    "name": getattr(fc, "name", ""),
                    "arguments": json.dumps(getattr(fc, "args", {}) or {}),
                },
            }
            tool_calls[idx] = tc
            seen_tool_starts.add(idx)
            events.append(
                ToolCallStart(index=idx, id=tc["id"], name=tc["function"]["name"])
            )
            events.append(ToolCallEnd(index=idx))
        elif hasattr(part, "thought") and part.thought:
            reasoning_buf.append(part.thought)
            events.append(ContentDelta(part.thought))

    return events


ENSURE_NO_RAW_BYTES = make_message_json_safe
