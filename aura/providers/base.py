from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

ProviderId = str  # Any registered provider key, e.g. "deepseek"
ThinkingMode = str  # "off" | "auto" | "high" | "max"
ModelId = str  # Any model string from any provider
ProviderKind = Literal["api_key", "external_cli", "local"]

#: The reasoning modes the user can pick, in display order.
#:
#: ``auto`` means *the provider decides* — Aura sends the provider's own
#: native automatic/adaptive reasoning mode, or omits the effort parameter so
#: the provider applies its documented default.  Aura never scores the task,
#: counts failures, or escalates on its own.  ``off``/``high``/``max`` stay
#: explicit user instructions and are always sent as selected.
THINKING_MODES: tuple[str, ...] = ("off", "auto", "high", "max")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    input_per_m_usd: float
    output_per_m_usd: float
    cache_hit_per_m_usd: float
    supports_vision: bool = False
    # Context metadata used to derive the per-request working-set budget.
    # 0 means "unknown" — callers fall back to the conservative defaults in
    # aura.conversation.context_budget rather than assuming a large window.
    # Both default to 0 so ModelInfo(**cached_dict) keeps working for caches
    # written before these fields existed.
    context_window_tokens: int = 0
    max_output_tokens: int = 0


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
        require_tool_call: bool = False,
    ) -> Iterator[Event]:
        ...
