"""Google Gen AI SDK client for Aura's Gemini API backend."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from typing import Any

try:
    from google import genai
    from google.genai import types as genai_types

    HAS_GOOGLE_GENAI = True
except ImportError:
    genai = None
    genai_types = None
    HAS_GOOGLE_GENAI = False

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
from aura.config import ThinkingMode

logger = logging.getLogger(__name__)

_DEFAULT_LOCATION = "us-central1"


class GeminiClient:
    """Gemini client backed by Google's unified ``google-genai`` SDK."""

    def __init__(self, credential: str | None = None, vertexai: bool = False) -> None:
        self.credential = credential
        self.vertexai = vertexai
        self.location = _first_env(
            "GOOGLE_CLOUD_LOCATION",
            "GCP_LOCATION",
            "GCP_REGION",
        ) or _DEFAULT_LOCATION

    def list_models(self) -> list[str]:
        return [m["id"] for m in self.fetch_raw_models() if m.get("id")]

    def fetch_raw_models(self) -> list[dict[str, Any]]:
        """Fetch the live model list through the Google Gen AI SDK.
        
        Note: Vertex AI does not support model listing via API keys (Express Mode).
        If we are in Vertex mode with an API key and the fetch fails, we fall back 
        to standard Google AI discovery to populate the list.
        """
        try:
            client = self._make_sdk_client()
            raw_models = client.models.list(config={"page_size": 300})
        except Exception as exc:
            # Discovery Bridge: If Vertex + API Key fails, try Google AI discovery
            if self.vertexai and _classify_google_credential(self.credential) == "api_key":
                logger.info("Vertex AI discovery failed (expected for API keys). Falling back to Google AI discovery.")
                try:
                    # Create a temporary non-vertex client just for discovery
                    fallback_kwargs = {"api_key": self.credential, "vertexai": False}
                    fallback_client = genai.Client(**fallback_kwargs)
                    raw_models = fallback_client.models.list(config={"page_size": 300})
                except Exception as fallback_exc:
                    msg = self._clean_error_msg(fallback_exc)
                    logger.error("Google AI discovery fallback also failed: %s", msg)
                    raise RuntimeError(f"Google Gen AI model discovery failed: {msg}") from fallback_exc
            else:
                msg = self._clean_error_msg(exc)
                logger.error("Google Gen AI model discovery failed: %s", msg)
                raise RuntimeError(f"Google Gen AI model discovery failed: {msg}") from exc

        models: list[dict[str, Any]] = []
        for raw in raw_models:
            model = _model_to_dict(raw)
            model_id = _model_id(model)
            if not model_id or "gemini" not in model_id.lower():
                continue
            model["id"] = model_id
            models.append(model)
        models.sort(key=lambda item: item["id"])
        return models

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        """Stream a Gemini response as Aura event objects."""
        if cancel_event is not None and cancel_event.is_set():
            yield ApiError(status_code=None, message="Cancelled.")
            return

        try:
            client = self._make_sdk_client()
            system_instruction, contents = _to_genai_contents(messages)
            config = _generation_config(thinking, temperature, tools or [], system_instruction)
            stream = client.models.generate_content_stream(
                model=_normalize_model_name(model),
                contents=contents,
                config=config,
            )
        except Exception as exc:
            yield ApiError(
                status_code=None,
                message=_redact_secret(f"{type(exc).__name__}: {self._clean_error_msg(exc)}", self.credential),
            )
            return

        content_buf: list[str] = []
        reasoning_buf: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: dict[str, int] | None = None
        thought_signature: str | None = None

        try:
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    break

                chunk_dict = _to_plain_dict(chunk)
                if chunk_dict.get("usage_metadata") or chunk_dict.get("usageMetadata"):
                    usage = _usage_from_metadata(
                        chunk_dict.get("usage_metadata") or chunk_dict.get("usageMetadata") or {}
                    )

                candidates = chunk_dict.get("candidates") or []
                if not candidates:
                    prompt_feedback = chunk_dict.get("prompt_feedback") or chunk_dict.get("promptFeedback") or {}
                    block_reason = prompt_feedback.get("block_reason") or prompt_feedback.get("blockReason")
                    if block_reason:
                        yield ApiError(
                            status_code=None,
                            message=f"Gemini blocked the prompt: {block_reason}",
                        )
                        return
                    text = chunk_dict.get("text")
                    if isinstance(text, str) and text:
                        content_buf.append(text)
                        yield ContentDelta(text)
                    continue

                candidate = candidates[0]
                finish_reason = _map_finish_reason(
                    candidate.get("finish_reason") or candidate.get("finishReason"),
                    finish_reason,
                )
                content = candidate.get("content") or {}
                for part in content.get("parts") or []:
                    if not isinstance(part, dict):
                        continue

                    # Capture thought_signature for Thinking models
                    sig = part.get("thought_signature") or part.get("thoughtSignature")
                    if sig:
                        thought_signature = sig

                    text = part.get("text")
                    if isinstance(text, str) and text:
                        if part.get("thought"):
                            reasoning_buf.append(text)
                            yield ReasoningDelta(text)
                        else:
                            content_buf.append(text)
                            yield ContentDelta(text)

                    function_call = part.get("function_call") or part.get("functionCall")
                    if isinstance(function_call, dict):
                        idx = len(tool_calls)
                        name = str(function_call.get("name") or "")
                        # PRESERVE native tool call ID if present; fallback to generated
                        call_id = str(function_call.get("id") or f"gemini_call_{idx}")
                        args = function_call.get("args") or {}
                        args_json = json.dumps(args, ensure_ascii=False)
                        call_signature = (
                            function_call.get("thought_signature")
                            or function_call.get("thoughtSignature")
                            or sig
                        )
                        tool_call = {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": args_json,
                            },
                        }
                        if call_signature:
                            tool_call["thought_signature"] = call_signature
                        tool_calls[idx] = tool_call
                        yield ToolCallStart(index=idx, id=call_id, name=name)
                        yield ToolCallArgsDelta(index=idx, args_chunk=args_json)
        except Exception as exc:
            yield ApiError(
                status_code=None,
                message=_redact_secret(f"{type(exc).__name__}: {self._clean_error_msg(exc)}", self.credential),
            )
            return

        for idx in sorted(tool_calls):
            yield ToolCallEnd(index=idx)

        if usage is not None:
            yield Usage(**usage)

        full_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_buf),
        }
        if reasoning_buf:
            full_message["reasoning_content"] = "".join(reasoning_buf)
        if thought_signature:
            full_message["thought_signature"] = thought_signature
        if tool_calls:
            full_message["tool_calls"] = [tool_calls[idx] for idx in sorted(tool_calls)]
            finish_reason = "tool_calls"

        yield Done(finish_reason=finish_reason, full_message=full_message)

    def _make_sdk_client(self) -> Any:
        if not HAS_GOOGLE_GENAI or genai is None:
            raise RuntimeError("google-genai is required for the Google Gemini provider.")

        kwargs: dict[str, Any] = {"vertexai": self.vertexai}
        if self.vertexai:
            # For Vertex AI, the credential can be a Project ID or an API Key
            kind = _classify_google_credential(self.credential)
            if kind == "api_key":
                kwargs["api_key"] = self.credential
            else:
                kwargs["project"] = self.credential
                kwargs["location"] = self.location
        else:
            kwargs["api_key"] = self.credential

        # Critical: If we are in Vertex mode with a Project ID, we MUST ensure 
        # the SDK doesn't pick up a stray GOOGLE_API_KEY from the environment.
        if self.vertexai and "project" in kwargs and "GOOGLE_API_KEY" in os.environ:
            kwargs["api_key"] = None

        return genai.Client(**kwargs)

    def _clean_error_msg(self, exc: Exception) -> str:
        msg = str(exc)
        if "PERMISSION_DENIED" in msg:
            if "generativelanguage.googleapis.com" in msg:
                return (
                    "403 Forbidden: The Gemini API is disabled in your project. "
                    "Enable it here: https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com"
                )
            if "aiplatform.googleapis.com" in msg:
                return (
                    "403 Forbidden: The Vertex AI or Agent Platform API is disabled, or your credentials lack permission. "
                    "1. Enable APIs: https://console.cloud.google.com/apis/library/aiplatform.googleapis.com "
                    "and https://console.cloud.google.com/apis/library/agentplatform.googleapis.com\n"
                    "2. Ensure you are using the correct region (default: us-central1).\n"
                    "3. If using a Project ID, ensure you have run 'gcloud auth application-default login' locally."
                )
        return msg


def _to_genai_contents(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}

    for msg in messages:
        role = msg.get("role")
        parts = _content_parts(msg.get("content"))

        if role == "system":
            system_parts.extend(str(part["text"]) for part in parts if "text" in part)
            continue

        if role == "assistant":
            model_parts: list[dict[str, Any]] = []
            
            # For Thinking models, preserve the model-returned thought signature
            # on the same kind of part where it was received.
            reasoning = msg.get("reasoning_content")
            thought_sig = msg.get("thought_signature")
            
            if reasoning:
                reasoning_part = {"text": str(reasoning), "thought": True}
                if thought_sig:
                    reasoning_part["thought_signature"] = thought_sig
                model_parts.append(reasoning_part)
            
            # If no reasoning but has other parts, start with parts
            model_parts.extend(parts)

            tool_calls = msg.get("tool_calls") or []
            attached_call_signature = False
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                name = str(fn.get("name") or "")
                call_id = str(tc.get("id") or "")
                if call_id and name:
                    tool_names_by_id[call_id] = name
                call_part = {
                    "function_call": {
                        "name": name,
                        "args": _json_object(fn.get("arguments")),
                    }
                }
                call_signature = tc.get("thought_signature") or fn.get("thought_signature")
                if not call_signature and thought_sig and not attached_call_signature:
                    call_signature = thought_sig
                if not call_signature and not attached_call_signature:
                    call_signature = "skip_thought_signature_validator"
                if call_signature:
                    call_part["thought_signature"] = str(call_signature)
                    attached_call_signature = True
                model_parts.append(call_part)
            
            if not model_parts:
                model_parts.append({"text": ""})

            # Gemini strictly forbids consecutive 'model' turns. 
            if contents and contents[-1]["role"] == "model":
                contents[-1]["parts"].extend(model_parts)
            else:
                contents.append({"role": "model", "parts": model_parts})
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            name = str(msg.get("name") or tool_names_by_id.get(call_id) or "tool")
            
            tool_part = {
                "function_response": {
                    "name": name,
                    "response": _tool_response(msg.get("content")),
                }
            }

            # Gemini requires ALL tool responses from a turn to be in ONE message.
            if contents and contents[-1]["role"] == "user":
                last_parts = contents[-1]["parts"]
                if last_parts and "function_response" in last_parts[0]:
                    last_parts.append(tool_part)
                    continue

            contents.append({"role": "user", "parts": [tool_part]})
            continue

        if role == "user":
            user_parts = parts or [{"text": ""}]
            # Group consecutive 'user' messages
            if contents and contents[-1]["role"] == "user":
                # Only group if the previous was NOT a tool response Turn
                prev_parts = contents[-1]["parts"]
                if prev_parts and "function_response" not in prev_parts[0]:
                    prev_parts.extend(user_parts)
                    continue

            contents.append({"role": "user", "parts": user_parts})

    return ("\n\n".join(system_parts) if system_parts else None), contents


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return []

    parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
            continue
        if item.get("type") == "image_url":
            image_url = item.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str):
                image_part = _image_url_part(url)
                if image_part:
                    parts.append(image_part)
    return parts


def _image_url_part(url: str) -> dict[str, Any] | None:
    if url.startswith("data:") and "," in url:
        header, data = url.split(",", 1)
        mime_type = header.split(":", 1)[1].split(";", 1)[0]
        return {"inline_data": {"mime_type": mime_type, "data": data}}
    if url.startswith(("gs://", "http://", "https://")):
        return {"file_data": {"mime_type": "image/jpeg", "file_uri": url}}
    return None


def _generation_config(
    thinking: ThinkingMode,
    temperature: float,
    tools: list[dict[str, Any]],
    system_instruction: str | None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "temperature": temperature,
        "candidate_count": 1,
    }
    if system_instruction:
        config["system_instruction"] = system_instruction

    genai_tools = _to_genai_tools(tools)
    if genai_tools:
        config["tools"] = genai_tools
        config["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}

    if thinking != "off":
        budget = 32000 if thinking == "max" else 8192
        config["thinking_config"] = {
            "include_thoughts": True,
            "thinking_budget": budget
        }
    
    # Minimize accidental blocks during code generation
    config["safety_settings"] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
    ]
    return config


def _to_genai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        declarations.append(
            {
                "name": str(fn["name"]),
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return [{"function_declarations": declarations}] if declarations else []


def _usage_from_metadata(raw: dict[str, Any]) -> dict[str, int]:
    prompt_tokens = int(raw.get("prompt_token_count") or raw.get("promptTokenCount") or 0)
    cache_hit_tokens = int(
        raw.get("cached_content_token_count") or raw.get("cachedContentTokenCount") or 0
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": int(
            raw.get("candidates_token_count") or raw.get("candidatesTokenCount") or 0
        ),
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": max(0, prompt_tokens - cache_hit_tokens),
    }


def _map_finish_reason(raw: Any, current: str | None) -> str | None:
    if not raw:
        return current
    value = str(raw).split(".")[-1].upper()
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "BLOCKLIST": "content_filter",
        "PROHIBITED_CONTENT": "content_filter",
        "MALFORMED_FUNCTION_CALL": "tool_calls",
        "UNEXPECTED_TOOL_CALL": "tool_calls",
        "TOO_MANY_TOOL_CALLS": "tool_calls",
    }
    return mapping.get(value, value.lower())


def _model_to_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        dumped = raw.model_dump(exclude_none=True)
        return dumped if isinstance(dumped, dict) else {}
    result: dict[str, Any] = {}
    for key in ("name", "base_model_id", "baseModelId", "display_name", "displayName"):
        value = getattr(raw, key, None)
        if value is not None:
            result[key] = value
    return result


def _to_plain_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "model_dump"):
        dumped = raw.model_dump(exclude_none=True)
        return dumped if isinstance(dumped, dict) else {}
    result: dict[str, Any] = {}
    for key in ("candidates", "usage_metadata", "prompt_feedback", "text"):
        value = getattr(raw, key, None)
        if value is not None:
            result[key] = value
    return _convert_plain(result)


def _convert_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _convert_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return _convert_plain(value.model_dump(exclude_none=True))
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
        return {
            k: _convert_plain(v)
            for k, v in vars(value).items()
            if not k.startswith("_") and v is not None
        }
    return value


def _normalize_model_name(model: str) -> str:
    name = model.strip()
    return name.rsplit("/models/", 1)[-1] if "/models/" in name else name.removeprefix("models/")


def _model_id(raw: dict[str, Any]) -> str:
    name = str(
        raw.get("name")
        or raw.get("base_model_id")
        or raw.get("baseModelId")
        or raw.get("id")
        or ""
    )
    if "/models/" in name:
        return name.rsplit("/models/", 1)[1]
    return name.removeprefix("models/")


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_response(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"result": content}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"result": content}


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _first_env_with_name(*names: str) -> tuple[str | None, str | None]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return name, value
    return None, None


def _classify_google_credential(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("AIza"):
        return "api_key"

    # Check for project:location format
    parts = value.split(":", 1)
    project = parts[0]

    # If the first part is numeric (Project Number), it's a project
    if project.isdigit():
        return "project"

    # If it looks like a Project ID (slug-like string)
    if _looks_like_google_project_id(project):
        return "project"

    return "api_key"


def _looks_like_google_project_id(value: str) -> bool:
    # Project Numbers are numeric
    if value.isdigit():
        return True

    # Project IDs are 6-30 chars, start with lowercase letter
    if len(value) < 6 or len(value) > 30:
        return False
    if not value[0].islower():
        return False
    if value.endswith("-"):
        return False
    return all(ch.islower() or ch.isdigit() or ch == "-" for ch in value)


def _redact_secret(message: str, secret: str | None) -> str:
    if not secret:
        return message
    return message.replace(secret, "[REDACTED]")
