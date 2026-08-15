from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

ProviderId = str  # Any registered provider key, e.g. "deepseek"
ThinkingMode = str  # "off" | "high" | "max"
ModelId = str  # Any model string from any provider
ProviderKind = Literal["api_key", "external_cli", "local"]

#: The reasoning modes the user can pick, in display order.
THINKING_MODES: tuple[str, ...] = ("off", "high", "max")


def normalize_thinking_mode(raw: object) -> ThinkingMode | None:
    """Normalize a stored or incoming thinking value.

    ``auto`` was a legacy user-facing mode. It is accepted only at this
    compatibility boundary and behaves exactly like the new High default.
    Unknown values remain invalid so callers can choose their own fallback.
    """
    if raw == "auto":
        return "high"
    if raw in THINKING_MODES:
        return raw  # type: ignore[return-value]
    return None


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    input_per_m_usd: float
    output_per_m_usd: float
    cache_hit_per_m_usd: float
    supports_vision: bool = False
    # Real model capacity, as the provider advertises it. Reporting metadata
    # only — nothing on the send path budgets, prunes, or compacts against it.
    # 0 means "unknown" (dynamically discovered or unlisted models). Both
    # default to 0 so ModelInfo(**cached_dict) keeps working for caches written
    # before these fields existed.
    context_window_tokens: int = 0
    max_output_tokens: int = 0
    # Upstream model-creation timestamp (unix seconds), as OpenRouter's
    # ``created`` field reports it. None means unknown/not provided — every
    # provider other than OpenRouter leaves this unset. Presentation code uses
    # it to order OpenRouter's list newest-first; nothing else reads it.
    created: int | None = None


@dataclass
class ProviderSpec:
    id: str
    label: str
    base_url: str
    env_key: str
    default_model: str
    default_thinking: ThinkingMode
    models: dict[str, ModelInfo]
    pricing: dict[str, dict[str, float]]
    kind: ProviderKind = "api_key"
    # Chat transport metadata. ``chat_protocol`` names the wire protocol used
    # for chat/tool turns — ``"openai_chat"`` (OpenAI-compatible Chat
    # Completions) or ``"anthropic_messages"`` (Anthropic Messages). It is read
    # once by the provider client; no call site infers it from the provider id.
    # ``chat_base_url`` overrides the API root used *only* for chat requests
    # (falls back to ``base_url``), so non-chat paths — model discovery,
    # pricing, native Responses web search — keep using the ordinary base URL.
    # ``requires_reasoning_replay`` says whether this transport needs prior
    # assistant reasoning re-encoded into the request to continue its thinking
    # across a tool round — true for DeepSeek (both transports) and native
    # Anthropic. It decides the *wire encoding* only, never what exists to
    # encode: canonical history keeps every assistant message's reasoning and
    # signature, ``History.for_api`` replays all of it unchanged, and the client
    # layer renders that same canonical reasoning into whatever the wire format
    # wants — provider-native ``reasoning_content``, an explicit DeepSeek Off
    # marker, or reconstructed Anthropic ``thinking`` blocks.
    chat_protocol: str = "openai_chat"
    chat_base_url: str | None = None
    requires_reasoning_replay: bool = True


class Event:
    """Minimal forward reference — real definition is in aura.client.events."""


@runtime_checkable
class ProviderClient(Protocol):
    """Protocol for API provider clients (OpenAI-compatible)."""

    def list_models(self) -> list[str]:
        ...

    def fetch_raw_models(self) -> list[dict[str, Any]]:
        ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: ThinkingMode,
        cancel_event: Any = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        ...
