"""Native web search through Aura's configured search backend.

Aura exposes a provider-neutral capability named ``web_search``.  Search is
owned by its own backend and is deliberately independent of whichever chat
model provider the user has selected:

- **Search provider** — always :data:`SEARCH_PROVIDER_ID` (DeepSeek).  Its
  credential and base URL are resolved explicitly here, so a chat model served
  by OpenRouter, OpenAI, Anthropic, Google, or DeepSeek all reach the same
  DeepSeek Responses API search path.
- **Chat provider** — carried through only as metadata for tracing.  It never
  decides whether search runs, and it never selects the search credential.

The legacy browser/Drone research path is retired and is never launched from
here; there is no fallback.
"""

from __future__ import annotations

import threading
from typing import Any

from aura.client.deepseek import DeepSeekClient
from aura.client.events import ApiError, ContentDelta, Done, Usage
from aura.config import ProviderId, resolve_api_key
from aura.research.result import ResearchResult

WEB_SEARCH_CAPABILITY = "web_search"

# Aura's configured web-search backend. This is NOT the chat model provider.
SEARCH_PROVIDER_ID: ProviderId = "deepseek"

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


def resolve_search_credential() -> str:
    """Return the API key for the search backend, independent of chat.

    Raises ``RuntimeError`` with the standard provider-configuration message
    when the DeepSeek key is missing.
    """
    return resolve_api_key(SEARCH_PROVIDER_ID)


def execute_native_web_search(
    *,
    question: str = "",
    context: str | None = None,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
    chat_provider: str | None = None,
) -> ResearchResult:
    """Run one native Responses API web search and normalize the result.

    Always executes against :data:`SEARCH_PROVIDER_ID` using that provider's
    own credential.  ``chat_provider`` is the user's selected chat model
    provider and is recorded for tracing only — it never gates execution.
    ``cancel_event`` must be the caller's existing turn cancel event.
    """
    question = str(question or "").strip()
    if not question:
        return ResearchResult(
            ok=False,
            status="invalid_request",
            error="web search question is required",
        )

    try:
        api_key = resolve_search_credential()
    except RuntimeError as exc:
        error = f"web search backend is not configured: {exc}"
        return ResearchResult(
            ok=False,
            status="failed",
            error=error,
            route_used=_route_used(model, chat_provider),
            summary="Native web search failed",
            gaps=[error],
        )

    client = DeepSeekClient(api_key=api_key, provider=SEARCH_PROVIDER_ID)
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

    partial_text = "".join(text_parts).strip()

    if api_error is not None:
        return _api_error_result(api_error, usage, partial_text, model, chat_provider)

    if payload is None:
        return ResearchResult(
            ok=False,
            answer=partial_text,
            usage=usage,
            status="failed",
            error="web search stream ended without a response",
            route_used=_route_used(model, chat_provider),
        )

    # The stream normally emits a Usage event; fall back to the usage the
    # final payload carries so token accounting is never silently lost.
    if usage is None and isinstance(payload.get("usage"), dict):
        usage = dict(payload["usage"])

    status = str(payload.get("status") or "").strip() or "failed"
    text = str(payload.get("text") or "").strip() or partial_text
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
        route_used=_route_used(model, chat_provider),
        summary=(
            f"Native web search returned {len(sources)} source(s)"
            if ok
            else f"Native web search ended with status '{status}'"
        ),
        error=error,
        run_id=str(payload.get("response_id") or ""),
        status=status,
        usage=usage,
    )


def _route_used(model: str | None, chat_provider: str | None) -> dict[str, Any]:
    return {
        "capability": WEB_SEARCH_CAPABILITY,
        "search_provider": SEARCH_PROVIDER_ID,
        "chat_provider": str(chat_provider or ""),
        "mode": _NATIVE_ROUTE,
        "model": model or "",
    }


def _api_error_result(
    api_error: ApiError,
    usage: dict[str, Any] | None,
    partial_text: str,
    model: str | None,
    chat_provider: str | None,
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
        answer=partial_text,
        usage=usage,
        status="failed",
        error=error,
        route_used=_route_used(model, chat_provider),
        summary="Native web search failed",
        gaps=[error],
    )
