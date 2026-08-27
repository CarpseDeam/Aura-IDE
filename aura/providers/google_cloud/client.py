import json
import threading
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from packaging.version import InvalidVersion, Version

from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    Usage,
)
from aura.client.hosted_search import (
    AURA_HOSTED_SEARCH_KEY,
    citation_from,
    citation_markdown,
    hosted_search_metadata,
)
from aura.providers.base import normalize_thinking_mode
from aura.providers.google_cloud.cooldown import CooldownManager
from aura.providers.google_cloud.errors import classify_error
from aura.providers.google_cloud.mapping import (
    aura_messages_to_google_contents,
    aura_tools_to_google_declarations,
)
from aura.providers.google_cloud.signatures import encode_signature_safe
from aura.providers.native_search import native_web_search_capability


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
        api_key: str | None = None,
    ) -> None:
        self._project = project
        self._location = location or "global"
        self._api_key = api_key
        self._client: Any = None
        self._cooldown = CooldownManager()
        self._call_metadata: dict[str, dict[str, Any]] = {}

    def _get_client(self) -> Any:
        """Lazily create and return the google-genai Client."""
        if self._client is None:
            import os

            from google import genai  # type: ignore[import-untyped]
            from google.genai import types

            # 120 seconds timeout (120,000 ms) to prevent hanging under unstable network
            http_options = types.HttpOptions(timeout=120000)
            use_vertex = bool(self._project) or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true")

            if use_vertex:
                self._client = genai.Client(
                    vertexai=True,
                    project=self._project,
                    location=self._location,
                    http_options=http_options,
                )
            elif self._api_key:
                self._client = genai.Client(
                    api_key=self._api_key,
                    http_options=http_options,
                )
            else:
                self._client = genai.Client(
                    vertexai=True,
                    project=self._project,
                    location=self._location,
                    http_options=http_options,
                )
        return self._client

    def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            models = client.models.list()
            return [m.name.removeprefix("models/") for m in models]
        except Exception:
            return []

    def fetch_raw_models(self) -> list[dict[str, Any]]:
        try:
            client = self._get_client()
            models = client.models.list()
            return [
                {
                    "id": m.name.removeprefix("models/"),
                    "name": getattr(m, "display_name", None) or m.name.removeprefix("models/"),
                }
                for m in models
            ]
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

        system_instruction, contents = aura_messages_to_google_contents(
            messages,
            google_call_metadata=self._call_metadata,
        )
        projected_tools, search_capability = _google_tool_projection(
            tools or [],
            model=model,
            transport_supports_combined_tools=(
                _google_combined_tools_transport_supported()
            ),
        )

        # Build config
        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if projected_tools:
            config_kwargs["tools"] = projected_tools
        thinking_config = _google_thinking_config(genai.types, model, thinking)
        if thinking_config is not None:
            config_kwargs["thinking_config"] = thinking_config
        if thinking == "off":
            config_kwargs["temperature"] = temperature

        # Prevent premature model termination by using maximum allowed output tokens and disabling safety blocks on code contents
        config_kwargs["max_output_tokens"] = 8192
        try:
            config_kwargs["safety_settings"] = [
                genai.types.SafetySetting(
                    category=genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    threshold=genai.types.HarmBlockThreshold.BLOCK_NONE,
                ),
                genai.types.SafetySetting(
                    category=genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    threshold=genai.types.HarmBlockThreshold.BLOCK_NONE,
                ),
                genai.types.SafetySetting(
                    category=genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    threshold=genai.types.HarmBlockThreshold.BLOCK_NONE,
                ),
                genai.types.SafetySetting(
                    category=genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                    threshold=genai.types.HarmBlockThreshold.BLOCK_NONE,
                ),
            ]
            # Try to add civic integrity category if supported by this SDK version
            try:
                config_kwargs["safety_settings"].append(
                    genai.types.SafetySetting(
                        category=genai.types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
                        threshold=genai.types.HarmBlockThreshold.BLOCK_NONE,
                    )
                )
            except AttributeError:
                pass
        except AttributeError:
            pass

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
        hosted_search_citations: list[dict[str, str]] = []
        hosted_search_queries: list[str] = []

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
                grounding_metadata = getattr(candidate, "grounding_metadata", None)
                if grounding_metadata is not None:
                    for raw_query in (
                        getattr(grounding_metadata, "web_search_queries", None) or []
                    ):
                        query = str(raw_query or "").strip()
                        if query and query not in hosted_search_queries:
                            hosted_search_queries.append(query)
                    for raw_chunk in (
                        getattr(grounding_metadata, "grounding_chunks", None) or []
                    ):
                        citation = citation_from(raw_chunk)
                        if citation is not None:
                            hosted_search_citations.append(citation)
                if getattr(candidate, "finish_reason", None):
                    finish_reason = candidate.finish_reason

                content = getattr(candidate, "content", None)
                if content is None or not hasattr(content, "parts") or content.parts is None:
                    continue

                for part in content.parts:
                    is_thought = bool(getattr(part, "thought", False))

                    # Thought text must stay out of the user-visible answer, but
                    # remains useful to preserve in Aura's reasoning stream.
                    if is_thought and hasattr(part, "text") and part.text:
                        text = part.text
                        reasoning_buf.append(text)
                        yield ReasoningDelta(text)
                        continue

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
                        call_id = getattr(fc, "id", None) or f"call_{idx}"
                        tc = {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": args_str},
                        }
                        thought_signature = getattr(part, "thought_signature", None)
                        if thought_signature:
                            self._call_metadata[call_id] = {
                                "thought_signature": encode_signature_safe(thought_signature)
                            }
                            # Also index by function name as fallback.
                            # The SDK may internally prefix call IDs (e.g. "default_api:"),
                            # so the call_id stored in the message history may not match
                            # the call_id used during streaming.  The fn: prefix avoids
                            # collisions with real call IDs.
                            if name:
                                self._call_metadata[f"fn:{name}"] = {
                                    "thought_signature": encode_signature_safe(thought_signature)
                                }
                        tool_calls[idx] = tc
                        seen_tool_starts.add(idx)
                        from aura.client.events import ToolCallArgsDelta, ToolCallEnd, ToolCallStart

                        yield ToolCallStart(index=idx, id=tc["id"], name=name)
                        
                        yield ToolCallArgsDelta(index=idx, args_chunk=args_str)
                        yield ToolCallEnd(index=idx)
                        continue

                    # Thought markers without text don't carry replayable content.
                    if is_thought:
                        continue

        except Exception as exc:
            yield _to_api_error(exc, self._cooldown)
            return

        # Build full message
        citation_suffix = citation_markdown(
            hosted_search_citations,
            "".join(content_buf),
        )
        if citation_suffix:
            content_buf.append(citation_suffix)
            yield ContentDelta(citation_suffix)

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
        metadata = hosted_search_metadata(
            provider="google_cloud",
            tool_type="google_search",
            citations=hosted_search_citations,
            calls=[
                {"query": query, "status": "completed"}
                for query in hosted_search_queries
            ],
        )
        if metadata is not None:
            full_message[AURA_HOSTED_SEARCH_KEY] = metadata

        yield Done(finish_reason=finish_reason, full_message=full_message)


def _google_combined_tools_transport_supported() -> bool:
    """Whether the installed google-genai transport supports tool mixing."""
    try:
        installed = Version(version("google-genai"))
    except (PackageNotFoundError, InvalidVersion):
        return False
    return installed > Version("2.0.0")


def _google_tool_projection(
    tools: list[dict[str, Any]],
    *,
    model: str,
    transport_supports_combined_tools: bool,
) -> tuple[list[dict[str, Any]], Any]:
    """Project hosted Google Search and Aura functions without tradeoffs."""
    declarations = aura_tools_to_google_declarations(tools)
    capability = native_web_search_capability(
        "google_cloud",
        model,
        transport="google_genai",
        transport_supports_combined_tools=transport_supports_combined_tools,
    )
    projected: list[dict[str, Any]] = []
    if capability is not None:
        projected.append(dict(capability.tool))
    if declarations:
        projected.append({"function_declarations": declarations})
    return projected, capability


def _google_thinking_config(types_module: Any, model: str, thinking: str) -> Any | None:
    """Map Aura thinking modes to Gemini thinking config where supported."""
    normalized = normalize_thinking_mode(thinking)
    if normalized is None:
        return None
    thinking = normalized
    model_l = model.lower()
    if "gemini-2.5" not in model_l:
        return None

    is_pro = "pro" in model_l
    if thinking == "off":
        # Gemini 2.5 Flash supports budget 0. Pro cannot disable thinking, so
        # use its documented minimum budget to keep latency as low as possible.
        budget = 128 if is_pro else 0
        return types_module.ThinkingConfig(
            thinking_budget=budget,
            include_thoughts=False,
        )
    if thinking == "high":
        return types_module.ThinkingConfig(
            thinking_budget=8192,
            include_thoughts=True,
        )
    if thinking == "max":
        return types_module.ThinkingConfig(
            thinking_budget=32768 if is_pro else 24576,
            include_thoughts=True,
        )
    return None


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
