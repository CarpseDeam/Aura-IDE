"""Provider-owned native web-search capability selection.

This is the only place Aura decides whether a selected production
provider/model/transport can expose hosted web search.  The result is immutable
and contains the exact server-tool projection for that request protocol.
Credentials are deliberately absent: native search shares the selected
provider request and therefore its existing authentication and lifecycle.

A model is admitted only when current official primary documentation says that
exact family supports that exact hosted tool.  An unsupported model simply
omits hosted search: there is no secondary request, no proxy, and no fallback
to another provider.

Read Only turns are included on purpose.  Hosted search is observational, it
lives inside the selected provider's own request, and it never reaches Aura's
ToolRunner — so it is compatible with Read Only, which is Aura's planning and
conversation mode.  Read Only still exposes only its existing local read/git
client tools; nothing here adds a client-side Aura ``web_search`` function.
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


# Model families that specialize in something other than text generation and
# never carry a hosted text web-search tool.
_SPECIALIZED_MODEL_TOKENS: tuple[str, ...] = (
    "audio",
    "image",
    "realtime",
    "transcribe",
    "tts",
)

# A trailing dated snapshot suffix, e.g. ``gpt-4.1-mini-2025-04-14``.
_DATED_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")

# Exactly the OpenAI models documented for the stable Responses ``web_search``
# tool, plus their size aliases. Anchored on both ends so no unrelated
# specialized variant is admitted by a shared prefix.
_OPENAI_RESPONSES_SEARCH_MODEL = re.compile(
    r"^(?:gpt-5(?:\.\d+)?(?:-(?:mini|nano|pro))?|gpt-4\.1(?:-mini)?|o4-mini)$"
)


def _base_model_id(model: str) -> str:
    """Return the lowercase model id with a dated snapshot suffix removed."""
    return _DATED_SNAPSHOT.sub("", str(model or "").strip().lower())


def _openai_responses_search_model(model: str) -> bool:
    """Models OpenAI documents for the stable Responses ``web_search`` tool.

    The documented surface is the GPT-5 family, GPT-4.1 and GPT-4.1 Mini, and
    ``o4-mini``.  Deliberately excluded:

    * GPT-4o — only the deprecated Chat Completions ``*-search-preview``
      models ever carried search for that family, never Responses
      ``web_search``;
    * GPT-4.1 Nano — the GPT-4.1 documentation covers GPT-4.1 and GPT-4.1
      Mini only, so a broad ``gpt-4.1`` prefix would over-claim;
    * ``gpt-5-search-api`` and ``gpt-5-chat-latest`` — a Chat Completions
      search model and a non-reasoning chat alias, neither documented for the
      Responses tool; and
    * audio, image, realtime, transcription, and TTS variants.

    Aliases and dated snapshots of a genuinely supported model are admitted.
    """
    base = _base_model_id(model)
    if any(token in base for token in _SPECIALIZED_MODEL_TOKENS):
        return False
    return bool(_OPENAI_RESPONSES_SEARCH_MODEL.match(base))


def _anthropic_dynamic_filtering_model(model: str) -> bool:
    return any(token in model for token in ("opus-4-6", "opus-4-7", "sonnet-4-6"))


__all__ = ["NativeWebSearchCapability", "native_web_search_capability"]
