"""Provider-owned native web-search capability selection.

This is the only place Aura decides whether a selected production
provider/model/transport can expose hosted web search.  The result is immutable
and contains the exact server-tool projection for that request protocol.
Credentials are deliberately absent: native search shares the selected
provider request and therefore its existing authentication and lifecycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

NativeSearchTransport = Literal[
    "responses",
    "anthropic_messages",
    "google_genai",
    "openai_chat",
    "external_cli",
]


@dataclass(frozen=True)
class NativeWebSearchCapability:
    """A frozen native-search projection for one provider/model transport."""

    provider: str
    model: str
    transport: NativeSearchTransport
    tool: dict[str, Any]


def native_web_search_capability(
    provider: str,
    model: str,
    *,
    transport: NativeSearchTransport,
    transport_supports_combined_tools: bool = True,
) -> NativeWebSearchCapability | None:
    """Return the truthful hosted-search surface for one production request.

    The function is intentionally pure.  For a turn's fixed provider, model,
    and transport feature set it always returns the same immutable decision,
    so client-tool continuations retain an identical native tool surface.
    """
    provider_id = str(provider or "").strip().lower()
    model_id = str(model or "").strip()
    model_l = model_id.lower()

    if transport == "responses":
        if provider_id == "deepseek" and model_l.startswith("deepseek-v4-"):
            return NativeWebSearchCapability(
                provider=provider_id,
                model=model_id,
                transport=transport,
                tool={"type": "web_search"},
            )
        if provider_id == "openai" and _openai_responses_search_model(model_l):
            return NativeWebSearchCapability(
                provider=provider_id,
                model=model_id,
                transport=transport,
                tool={"type": "web_search"},
            )
        return None

    if transport == "anthropic_messages" and provider_id == "anthropic":
        if not re.match(r"^claude-(?:opus|sonnet|haiku)-(?:4|5)(?:-|$)", model_l):
            return None
        # Claude 4.6+ can use the current dynamic-filtering version.  The basic
        # version remains the honest surface for earlier supported Claude 4.x
        # models in Aura's catalog.
        version = (
            "web_search_20260209"
            if _anthropic_dynamic_filtering_model(model_l)
            else "web_search_20250305"
        )
        return NativeWebSearchCapability(
            provider=provider_id,
            model=model_id,
            transport=transport,
            tool={"type": version, "name": "web_search"},
        )

    if transport == "google_genai" and provider_id == "google_cloud":
        if (
            transport_supports_combined_tools
            and model_l.startswith("gemini-3")
            and not any(token in model_l for token in ("image", "live", "tts"))
        ):
            return NativeWebSearchCapability(
                provider=provider_id,
                model=model_id,
                transport=transport,
                tool={"google_search": {}},
            )
        return None

    if transport == "openai_chat" and provider_id == "openrouter":
        return NativeWebSearchCapability(
            provider=provider_id,
            model=model_id,
            transport=transport,
            tool={"type": "openrouter:web_search"},
        )

    # External CLIs own their own hosted capabilities.  No Aura server tool is
    # projected into those turns, and no other provider receives a fallback.
    return None


def _openai_responses_search_model(model: str) -> bool:
    """Models Aura can truthfully route through Responses hosted search."""
    return (
        model.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4"))
        and not any(
            token in model
            for token in ("audio", "image", "realtime", "transcribe", "tts")
        )
    )


def _anthropic_dynamic_filtering_model(model: str) -> bool:
    return any(token in model for token in ("opus-4-6", "opus-4-7", "sonnet-4-6"))


__all__ = ["NativeWebSearchCapability", "native_web_search_capability"]
