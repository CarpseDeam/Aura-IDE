from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Protocol, runtime_checkable

ProviderId = str  # Any registered provider key, e.g. "deepseek"
ThinkingMode = str  # "off" | "high" | "max"
ModelId = str  # Any model string from any provider


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    input_per_m_usd: float
    output_per_m_usd: float
    cache_hit_per_m_usd: float
    supports_vision: bool = False


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


class Event:
    """Minimal forward reference — real definition is in aura.client.events."""
    pass


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
