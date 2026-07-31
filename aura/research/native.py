"""Native web search through the active provider's Responses API.

Aura exposes a provider-neutral capability named ``web_search``.  When the
active provider supports a native Responses API web-search tool — currently
only DeepSeek — execution translates that capability to the built-in
``{"type": "web_search"}`` tool and streams a Responses request through the
existing OpenAI-compatible client.

Provider capability handling is explicit: providers without a native
compatible web-search tool get a clear unsupported result.  The legacy
browser/Drone research path is never launched from here.
"""

from __future__ import annotations

import threading
from typing import Any

from aura.client.deepseek import DeepSeekClient
from aura.client.events import ApiError, ContentDelta, Done, Usage
from aura.client.responses_stream import build_native_web_search_request
from aura.config import ProviderId
from aura.research.result import ResearchResult

WEB_SEARCH_CAPABILITY = "web_search"

# Providers that expose a native Responses API web-search built-in.
NATIVE_WEB_SEARCH_PROVIDERS: frozenset[str] = frozenset({"deepseek"})

# User-facing messages for common Responses API HTTP failures.
_HTTP_STATUS_MESSAGES: dict[int, str] = {
    400: "the provider rejected the web search request",
    401: "authentication failed — the provider API key is invalid or missing",
    402: "the provider account requires payment before web search can run",
    429: "rate limited — the provider is throttling web search requests",
    500: "the provider reported an internal error during web search",
    503: "the provider service is unavailable — retry later",
}

_NATIVE_ROUTE = "native_responses_web_search"


def supports_native_web_search(provider: str | None) -> bool:
    """Explicit capability check for the native web-search tool."""
    return bool(provider) and provider in NATIVE_WEB_SEARCH_PROVIDERS


def unsupported_web_search_result(provider: str | None) -> ResearchResult:
    """Clear unsupported result for providers without native web search."""
    provider_name = provider or "unknown"
    return ResearchResult(
        ok=False,
        answer="",
        confidence="none",
        route_used={
            "capability": WEB_SEARCH_CAPABILITY,
            "provider": provider_name,
            "mode": "unsupported",
        },
        status="unsupported",
        error=(
            f"web_search is not supported by provider '{provider_name}': "
            f"no native web search tool is configured. "
            f"Native web search requires a provider with a Responses API "
            f"web-search built-in (currently: {', '.join(sorted(NATIVE_WEB_SEARCH_PROVIDERS))})."
        ),
    )


def execute_native_web_search(
    *,
    provider: ProviderId = "deepseek",
    question: str = "",
    context: str | None = None,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
) -> ResearchResult:
    """Run one native Responses API web search and normalize the result.

    Uses the existing OpenAI-compatible client configuration (api_key from
    the provider key store, base_url from the provider catalog).  The raw
    Responses stream is consumed here; DeepSeek-specific response events
    never leave the client layer.
    """
    question = str(question or "").strip()
    if not question:
        return ResearchResult(
            ok=False,
            status="invalid_request",
            error="web search question is required",
        )

    if not supports_native_web_search(provider):
        return unsupported_web_search_result(provider)

    client = DeepSeekClient(provider=provider)
    text_parts: list[str] = []
    usage: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    api_error: ApiError | None = None

    for event in client.stream_responses_web_search(
        question=question,
        context=context,
        model=model,
        cancel_event=cancel_event,
    ):
        if isinstance(event, ContentDelta):
            text_parts.append(event.text)
        elif isinstance(event, Usage):
            usage = {
                "prompt_tokens": event.prompt_tokens,
                "completion_tokens": event.completion_tokens,
                "cache_hit_tokens": event.cache_hit_tokens,
                "cache_miss_tokens": event.cache_miss_tokens,
            }
        elif isinstance(event, Done):
            payload = event.full_message if isinstance(event.full_message, dict) else {}
        elif isinstance(event, ApiError):
            api_error = event

    if api_error is not None:
        return _api_error_result(provider, api_error, question, usage)

    if payload is None:
        return ResearchResult(
            ok=False,
            answer="\n".join(text_parts).strip(),
            usage=usage,
            status="failed",
            error="web search stream ended without a response",
            route_used=_route_used(provider, model),
        )

    status = str(payload.get("status") or "in_progress")
    text = str(payload.get("text") or "").strip() or "\n".join(text_parts).strip()
    sources = payload.get("sources") or []
    if not isinstance(sources, list):
        sources = []
    search_calls = payload.get("web_search_calls") or []
    if not isinstance(search_calls, list):
        search_calls = []

    error = str(payload.get("error") or "").strip()
    incomplete_reason = str(payload.get("incomplete_reason") or "").strip()
    if status == "incomplete":
        error = error or (
            "web search response was incomplete"
            + (f" ({incomplete_reason})" if incomplete_reason else "")
        )
    elif status == "failed":
        error = error or "web search response failed"
    elif status == "cancelled":
        error = error or "web search was cancelled"
    elif status != "completed":
        error = error or f"web search ended without a terminal status ('{status}')"

    ok = status == "completed" and not error
    confidence = "high" if ok and sources else "none"
    return ResearchResult(
        ok=ok,
        answer=text,
        sources=[dict(s) for s in sources],
        evidence=[
            {"kind": "web_search_call", **dict(c)} for c in search_calls
        ],
        verified_facts=[],
        gaps=[] if ok else [error or f"web search ended with status '{status}'"],
        confidence=confidence,
        trace=[
            {
                "stage": "native_web_search",
                "status": status,
                "response_id": str(payload.get("response_id") or ""),
            }
        ],
        route_used=_route_used(provider, model),
        summary=(
            f"Native web search returned {len(sources)} source(s)"
            if ok
            else f"Native web search ended with status '{status}'"
        ),
        error=error,
        run_id=str(payload.get("response_id") or ""),
        status=status,
    )


def _route_used(provider: str, model: str | None) -> dict[str, Any]:
    return {
        "capability": WEB_SEARCH_CAPABILITY,
        "provider": provider,
        "mode": _NATIVE_ROUTE,
        "model": model or "",
    }


def _api_error_result(
    provider: str,
    api_error: ApiError,
    question: str,
    usage: dict[str, Any] | None,
) -> ResearchResult:
    code = api_error.status_code
    hint = _HTTP_STATUS_MESSAGES.get(code) if code is not None else ""
    message = str(api_error.message or "").strip()
    detail = f" ({message})" if message else ""
    error = (
        f"web search failed with HTTP {code}: {hint}{detail}"
        if code is not None
        else f"web search failed: {message or 'unknown error'}"
    )
    return ResearchResult(
        ok=False,
        answer="",
        usage=usage,
        status="failed",
        error=error,
        route_used=_route_used(provider, None),
        summary="Native web search failed",
        gaps=[error],
    )
