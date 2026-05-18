import json
import threading
from collections.abc import Iterator
from typing import Any

from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    Usage,
)
from aura.providers.google_cloud.cooldown import CooldownManager
from aura.providers.google_cloud.errors import classify_error
from aura.providers.google_cloud.mapping import (
    aura_messages_to_google_contents,
    aura_tools_to_google_declarations,
)


class GoogleCloudClient:
    """Streaming client for Google Cloud / Vertex AI via the google-genai SDK.

    All google-genai imports are deferred so the module can be imported
    without the optional dependency installed.
    """

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
    ) -> None:
        self._project = project
        self._location = location or "global"
        self._client: Any = None
        self._cooldown = CooldownManager()

    def _get_client(self) -> Any:
        """Lazily create and return the google-genai Client."""
        if self._client is None:
            from google import genai  # type: ignore[import-untyped]

            self._client = genai.Client(
                vertexai=True,
                project=self._project,
                location=self._location,
            )
        return self._client

    def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            models = client.models.list()
            return [m.name for m in models]
        except Exception:
            return []

    def fetch_raw_models(self) -> list[dict[str, Any]]:
        try:
            client = self._get_client()
            models = client.models.list()
            return [{"id": m.name, "name": getattr(m, "display_name", None) or m.name} for m in models]
        except Exception:
            return []

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: str,
        cancel_event: threading.Event | None = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        # Cooldown check
        if self._cooldown.is_cooling():
            yield ApiError(
                status_code=429,
                message="In cooldown after previous rate-limit hit — "
                f"{self._cooldown.remaining():.0f}s remaining.",
            )
            return

        try:
            from google import genai  # type: ignore[import-untyped]
        except ImportError:
            yield ApiError(
                status_code=None,
                message="google-genai package is not installed. "
                "Install it with: pip install google-genai",
            )
            return

        system_instruction, contents = aura_messages_to_google_contents(messages)
        google_tools = aura_tools_to_google_declarations(tools or [])

        # Build config
        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if google_tools:
            config_kwargs["tools"] = [{"function_declarations": google_tools}]
        if thinking == "off":
            config_kwargs["temperature"] = temperature

        config = genai.types.GenerateContentConfig(**config_kwargs)

        # State accumulators
        content_buf: list[str] = []
        reasoning_buf: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        seen_tool_starts: set[int] = set()
        finish_reason: str | None = None
        usage_emitted = False
        prompt_tokens = 0
        completion_tokens = 0

        try:
            client = self._get_client()
            stream = client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            yield _to_api_error(exc, self._cooldown)
            return

        try:
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    break

                # Usage metadata
                if not usage_emitted and hasattr(chunk, "usage_metadata") and chunk.usage_metadata is not None:
                    um = chunk.usage_metadata
                    prompt_tokens = getattr(um, "prompt_token_count", 0) or 0
                    completion_tokens = getattr(um, "candidates_token_count", 0) or 0
                    if prompt_tokens or completion_tokens:
                        yield Usage(
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            cache_hit_tokens=0,
                            cache_miss_tokens=0,
                        )
                        usage_emitted = True

                if not hasattr(chunk, "candidates") or not chunk.candidates:
                    continue

                candidate = chunk.candidates[0]
                if getattr(candidate, "finish_reason", None):
                    finish_reason = candidate.finish_reason

                content = getattr(candidate, "content", None)
                if content is None or not hasattr(content, "parts"):
                    continue

                for part in content.parts:
                    # Text
                    if hasattr(part, "text") and part.text:
                        text = part.text
                        content_buf.append(text)
                        yield ContentDelta(text)
                        continue

                    # Function call
                    if hasattr(part, "function_call") and part.function_call is not None:
                        fc = part.function_call
                        idx = len(tool_calls)
                        name = getattr(fc, "name", "")
                        args = getattr(fc, "args", None)
                        if args is None:
                            args = {}
                        args_str = json.dumps(args)
                        tc = {
                            "id": getattr(fc, "id", f"call_{idx}"),
                            "type": "function",
                            "function": {"name": name, "arguments": args_str},
                        }
                        tool_calls[idx] = tc
                        seen_tool_starts.add(idx)
                        from aura.client.events import ToolCallEnd, ToolCallStart

                        yield ToolCallStart(index=idx, id=tc["id"], name=name)
                        yield ToolCallEnd(index=idx)
                        continue

                    # Thought (reasoning)
                    if hasattr(part, "thought") and part.thought:
                        reasoning_buf.append(part.thought)
                        continue

        except Exception as exc:
            yield _to_api_error(exc, self._cooldown)
            return

        # Build full message
        full_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_buf),
            "reasoning_content": "".join(reasoning_buf),
        }
        if not full_message["reasoning_content"]:
            full_message.pop("reasoning_content")
        if tool_calls:
            full_message["tool_calls"] = [
                tool_calls[i] for i in sorted(tool_calls)
            ]
            for tc in full_message["tool_calls"]:
                if not tc["function"]["arguments"]:
                    tc["function"]["arguments"] = "{}"
                else:
                    try:
                        json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        pass

        yield Done(finish_reason=finish_reason, full_message=full_message)


def _to_api_error(exc: Exception, cooldown: CooldownManager) -> ApiError:
    """Convert an exception to an ApiError, with 429 cooldown handling."""
    try:
        from google.genai import errors as genai_errors  # type: ignore[import-untyped]
    except ImportError:
        return ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")

    status_code: int | None = None
    message = ""

    if isinstance(exc, genai_errors.ClientError):
        status_code = getattr(exc, "status_code", None)
        message = str(exc)
    else:
        return ApiError(status_code=None, message=f"{type(exc).__name__}: {exc}")

    if status_code == 429:
        cooldown.hit()

    if status_code is not None:
        gc_err = classify_error(status_code, message)
        # Don't leak credential details
        if gc_err.status_code in (401, 403):
            message = "Authentication failed. Check your Google Cloud credentials."
        else:
            message = str(gc_err)

    return ApiError(status_code=status_code, message=message)
