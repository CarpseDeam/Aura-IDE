"""Compatibility layer — re-exports from aura.providers.

All provider-specific logic lives in ``aura/providers/``.  This module
exists so that existing imports continue to work without changes.
"""

from __future__ import annotations

from aura.providers.base import ProviderId, ThinkingMode  # noqa: F401
from aura.providers.base import ProviderSpec as ProviderConfig
from aura.providers.catalog import DEFAULT_MODEL, DEFAULT_THINKING  # noqa: F401
from aura.providers.registry import provider_registry

ModelId = str  # Any model string from any provider

# Build the PROVIDERS dict from the registry for backward compatibility.
PROVIDERS: dict[ProviderId, ProviderConfig] = provider_registry.all()  # type: ignore[assignment]


def get_pricing(model_id: str) -> dict[str, float] | None:
    for provider in PROVIDERS.values():
        if model_id in provider.pricing:
            return provider.pricing[model_id]
    # Fallback: read from ModelInfo pricing fields (used by dynamically
    # fetched models that store pricing in ModelInfo but not in the pricing dict).
    for provider in PROVIDERS.values():
        if model_id in provider.models:
            mi = provider.models[model_id]
            return {
                "in_miss": mi.input_per_m_usd,
                "in_hit": mi.cache_hit_per_m_usd,
                "out": mi.output_per_m_usd,
            }
    return None


def cost_usd(
    model: str,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    output_tokens: int,
) -> float | None:
    p = get_pricing(model)
    if p is None:
        return None
    return (
        cache_hit_tokens * p["in_hit"]
        + cache_miss_tokens * p["in_miss"]
        + output_tokens * p["out"]
    ) / 1_000_000
