from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderId = Literal["deepseek", "openai", "google", "openrouter", "anthropic"]
ThinkingMode = Literal["off", "high", "max"]
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
class ProviderConfig:
    id: ProviderId
    label: str
    base_url: str
    env_key: str
    default_model: str
    default_thinking: ThinkingMode
    models: dict[str, ModelInfo]
    pricing: dict[str, dict[str, float]]


# ---------------------------------------------------------------------------
# Provider catalogues (Dynamically Loaded)
# ---------------------------------------------------------------------------

# These are initialized empty. The application populates them from local 
# discovery cache or by fetching from provider APIs at runtime.

DEEPSEEK_MODELS: dict[str, ModelInfo] = {}
DEEPSEEK_PRICING: dict[str, dict[str, float]] = {}

OPENAI_MODELS: dict[str, ModelInfo] = {}
OPENAI_PRICING: dict[str, dict[str, float]] = {}

GOOGLE_MODELS: dict[str, ModelInfo] = {}
GOOGLE_PRICING: dict[str, dict[str, float]] = {}

ANTHROPIC_MODELS: dict[str, ModelInfo] = {}
ANTHROPIC_PRICING: dict[str, dict[str, float]] = {}

OPENROUTER_MODELS: dict[str, ModelInfo] = {}
OPENROUTER_PRICING: dict[str, dict[str, float]] = {}

PROVIDERS: dict[ProviderId, ProviderConfig] = {
    "deepseek": ProviderConfig(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-v4-flash",
        default_thinking="high",
        models=DEEPSEEK_MODELS,
        pricing=DEEPSEEK_PRICING,
    ),
    "openai": ProviderConfig(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        default_model="gpt-4o",
        default_thinking="off",
        models=OPENAI_MODELS,
        pricing=OPENAI_PRICING,
    ),
    "google": ProviderConfig(
        id="google",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        env_key="GEMINI_API_KEY",
        default_model="gemini-2.0-flash",
        default_thinking="off",
        models=GOOGLE_MODELS,
        pricing=GOOGLE_PRICING,
    ),
    "openrouter": ProviderConfig(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        default_model="openai/gpt-4o",
        default_thinking="off",
        models=OPENROUTER_MODELS,
        pricing=OPENROUTER_PRICING,
    ),
    "anthropic": ProviderConfig(
        id="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com/v1",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-6",
        default_thinking="high",
        models=ANTHROPIC_MODELS,
        pricing=ANTHROPIC_PRICING,
    ),
}

def get_pricing(model_id: str) -> dict[str, float] | None:
    for provider in PROVIDERS.values():
        if model_id in provider.pricing:
            return provider.pricing[model_id]
    return None


def cost_usd(
    model: str,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    output_tokens: int,
) -> float:
    p = get_pricing(model)
    if p is None:
        return 0.0
    return (
        cache_hit_tokens * p["in_hit"]
        + cache_miss_tokens * p["in_miss"]
        + output_tokens * p["out"]
    ) / 1_000_000

# ---------------------------------------------------------------------------
# Default model/thinking constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL: str = "deepseek-v4-flash"
DEFAULT_THINKING: ThinkingMode = "high"
DEFAULT_PLANNER_MODEL: str = "deepseek-v4-flash"
DEFAULT_WORKER_MODEL: str = "deepseek-v4-pro"
DEFAULT_PLANNER_THINKING: ThinkingMode = "off"
DEFAULT_WORKER_THINKING: ThinkingMode = "high"
