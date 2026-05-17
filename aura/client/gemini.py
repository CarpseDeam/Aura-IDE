"""Native Gemini REST client for Aura's API backend."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import httpx

from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from aura.config import ThinkingMode, get_provider, resolve_api_key


class GeminiClient:
    """Native Gemini API client using streamGenerateContent."""

    def __init__(self, api_key: str | None = None) -> None:
        cfg = get_provider("google")
        self._api_key = api_key if api_key is not None else resolve_api_key("google")
        self._base_url = cfg.base_url.rstrip("/")

    def list_models(self) -> list[str]:
        return [
            _model_id(raw)
            for raw in self.fetch_raw_models()
            if "generateContent" in (raw.get("supportedGenerationMethods") or [])
        ]

    def fetch_raw_models(self) -> list[dict[str, Any]]:
        try:
            headers = {"x-goog-api-key": self._api_key}
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self._base_url}/models",
                    params={"pageSize": 1000},
                    headers=headers,
                )
                resp.raise_for_status()
                models = resp.json().get("models", [])
                for raw in models:
                    if isinstance(raw, dict):
                        raw.setdefault("id", _model_id(raw))
                return models
        except Exception:
            return []

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        if cancel_event is not None and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        normalized_model = _normalize_model_name(model)
        system_instruction, contents = _to_gemini_contents(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generation_config": _generation_config(thinking, temperature),
        }
        if system_instruction is not None:
            body["system_instruction"] = system_instruction
        gemini_tools = _to_gemini_tools(tools or [])
        if gemini_tools:
            body["tools"] = gemini_tools
            body["tool_config"] = {
                "function_calling_config": {"mode": "auto"},
            }

        content_buf: list[str] = []
        reasoning_buf: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: dict[str, int] | None = None

        timeout = httpx.Timeout(120.0, connect=10.0, read=None)
        try:
            # Use manual URL construction and x-goog-api-key header to bypass 
            # httpx query parameter bugs with colons in the path.
            url = f"{self._base_url}/models/{normalized_model}:streamGenerateContent?alt=sse"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            }

            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=body,
                ) as response:
                    response.raise_for_status()
                    for chunk in _iter_sse_json(response):
                        if cancel_event is not None and cancel_event.is_set():
                            break

                        if chunk.get("usageMetadata"):
                            usage = _usage_from_metadata(chunk["usageMetadata"])

                        candidates = chunk.get("candidates") or []
                        if not candidates:
                            prompt_feedback = chunk.get("promptFeedback") or {}
                            block_reason = prompt_feedback.get("blockReason")
                            if block_reason:
                                yield ApiError(
                                    status_code=None,
                                    message=f"Gemini blocked the prompt: {block_reason}",
                                )
                                return
                            continue

                        candidate = candidates[0]
                        finish_reason = _map_finish_reason(
                            candidate.get("finishReason"),
                            finish_reason,
                        )
                        parts = (candidate.get("content") or {}).get("parts") or []
                        for part in parts:
                            text = part.get("text")
                            if text:
                                if part.get("thought"):
                                    reasoning_buf.append(text)
                                    yield ReasoningDelta(text)
                                else:
                                    content_buf.append(text)
                                    yield ContentDelta(text)

                            function_call = (
                                part.get("function_call")
                                or part.get("functionCall")
                            )
                            if isinstance(function_call, dict):
                                idx = len(tool_calls)
                                name = str(function_call.get("name") or "")
                                args = function_call.get("args") or {}
                                args_json = json.dumps(args, ensure_ascii=False)
                                tool_call = {
                                    "id": f"gemini_call_{idx}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": args_json,
                                    },
                                }
                                tool_calls[idx] = tool_call
                                yield ToolCallStart(
                                    index=idx,
                                    id=tool_call["id"],
                                    name=name,
                                )
                                yield ToolCallArgsDelta(index=idx, args_chunk=args_json)
        except httpx.HTTPStatusError as exc:
            msg = _redact_api_key(str(exc), self._api_key)
            # Add version marker [V2] to identify this fixed code
            yield ApiError(status_code=exc.response.status_code, message=f"[V2] {msg}")
            return
        except Exception as exc:
            msg = _redact_api_key(f"{type(exc).__name__}: {exc}", self._api_key)
            yield ApiError(status_code=None, message=f"[V2] {msg}")
            return

        for idx in sorted(tool_calls):
            yield ToolCallEnd(index=idx)

        if usage is not None:
            yield Usage(**usage)

        full_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_buf),
            "reasoning_content": "".join(reasoning_buf),
        }
        if not full_message["reasoning_content"]:
            full_message.pop("reasoning_content")
        if tool_calls:
            full_message["tool_calls"] = [tool_calls[idx] for idx in sorted(tool_calls)]
            finish_reason = "tool_calls"

        yield Done(finish_reason=finish_reason, full_message=full_message)


def _model_id(raw: dict[str, Any]) -> str:
    name = str(raw.get("name") or raw.get("baseModelId") or "")
    return name.removeprefix("models/")


def _generation_config(thinking: ThinkingMode, temperature: float) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if thinking == "off":
        config["temperature"] = temperature
    else:
        config["thinking_config"] = {
            "include_thoughts": True,
            "thinking_level": "HIGH",
        }
        if thinking == "max":
            config["thinking_config"]["thinking_budget"] = 32768
    return config


def _to_gemini_contents(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    system_parts: list[dict[str, Any]] = []
    contents: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            parts = _message_content_to_parts(msg.get("content"))
            system_parts.extend(part for part in parts if "text" in part)
            continue

        if role == "tool":
            tool_call_id = str(msg.get("tool_call_id") or "")
            name = tool_names_by_id.get(tool_call_id, tool_call_id or "tool_result")
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": name,
                                "response": _tool_response_payload(msg.get("content")),
                            }
                        }
                    ],
                }
            )
            continue

        if role not in ("user", "assistant"):
            continue

        gemini_role = "model" if role == "assistant" else "user"
        parts = _message_content_to_parts(msg.get("content"))

        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                call_id = str(tc.get("id") or "")
                name = str(fn.get("name") or "")
                if call_id and name:
                    tool_names_by_id[call_id] = name
                parts.append(
                    {
                        "function_call": {
                            "name": name,
                            "args": _parse_json_object(fn.get("arguments")),
                        }
                    }
                )

        if not parts:
            parts = [{"text": ""}]
        contents.append({"role": gemini_role, "parts": parts})

    system_instruction = {"parts": system_parts} if system_parts else None
    return system_instruction, contents


def _message_content_to_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []

    parts: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return parts

    for item in content:
        if not isinstance(item, dict):
            continue
        ptype = item.get("type")
        if ptype == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
        elif ptype == "image_url":
            data_url = (item.get("image_url") or {}).get("url")
            inline_data = _data_url_to_inline_data(data_url)
            if inline_data is not None:
                parts.append({"inline_data": inline_data})
    return parts


def _data_url_to_inline_data(data_url: Any) -> dict[str, str] | None:
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        header, data = data_url.split(",", 1)
        mime_type = header.split(":", 1)[1].split(";", 1)[0]
    except (IndexError, ValueError):
        return None
    if not mime_type or not data:
        return None
    return {"mime_type": mime_type, "data": data}


def _parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_response_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"result": raw}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"result": raw}


def _to_gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        declaration: dict[str, Any] = {
            "name": name,
            "description": str(fn.get("description") or ""),
        }
        parameters = fn.get("parameters")
        if isinstance(parameters, dict):
            declaration["parameters"] = parameters
        declarations.append(declaration)
    return [{"function_declarations": declarations}] if declarations else []


def _iter_sse_json(response: httpx.Response) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for line in response.iter_lines():
        if not line:
            yield from _flush_sse_json(data_lines)
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    yield from _flush_sse_json(data_lines)


def _flush_sse_json(data_lines: list[str]) -> Iterator[dict[str, Any]]:
    if not data_lines:
        return
    data = "\n".join(data_lines)
    data_lines.clear()
    if data == "[DONE]":
        return
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return
    if isinstance(parsed, dict):
        yield parsed


def _usage_from_metadata(raw: dict[str, Any]) -> dict[str, int]:
    prompt_tokens = int(raw.get("promptTokenCount") or 0)
    cache_hit_tokens = int(raw.get("cachedContentTokenCount") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": int(raw.get("candidatesTokenCount") or 0),
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": max(0, prompt_tokens - cache_hit_tokens),
    }


def _map_finish_reason(raw: Any, current: str | None) -> str | None:
    if not raw:
        return current
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "MALFORMED_FUNCTION_CALL": "tool_calls",
        "UNEXPECTED_TOOL_CALL": "tool_calls",
        "TOO_MANY_TOOL_CALLS": "tool_calls",
    }
    return mapping.get(str(raw), str(raw).lower())


def _normalize_model_name(name: str) -> str:
    """Accept both 'gemini-1.5-pro' and 'models/gemini-1.5-pro'."""
    if name.startswith("models/"):
        return name.removeprefix("models/")
    return name


def _redact_api_key(message: str, api_key: str | None) -> str:
    """Redact the API key from any error message or URL."""
    if not api_key or not message:
        return message
    return message.replace(api_key, "[REDACTED]")
