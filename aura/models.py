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
# Provider catalogues
# ---------------------------------------------------------------------------

DEEPSEEK_MODELS: dict[str, ModelInfo] = {
    "deepseek-v4-flash": ModelInfo(
        id="deepseek-v4-flash",
        label="V4 Flash",
        input_per_m_usd=0.14,
        output_per_m_usd=0.28,
        cache_hit_per_m_usd=0.014,
    ),
    "deepseek-v4-pro": ModelInfo(
        id="deepseek-v4-pro",
        label="V4 Pro",
        input_per_m_usd=0.55,
        output_per_m_usd=2.19,
        cache_hit_per_m_usd=0.07,
    ),
}

DEEPSEEK_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"in_miss": 0.14, "in_hit": 0.003, "out": 0.28},
    "deepseek-v4-pro": {"in_miss": 1.74, "in_hit": 0.015, "out": 3.48},
}

OPENAI_MODELS: dict[str, ModelInfo] = {
    "gpt-4o": ModelInfo(
        id="gpt-4o",
        label="GPT-4o",
        input_per_m_usd=2.50,
        output_per_m_usd=10.00,
        cache_hit_per_m_usd=1.25,
        supports_vision=True,
    ),
    "gpt-4o-mini": ModelInfo(
        id="gpt-4o-mini",
        label="GPT-4o Mini",
        input_per_m_usd=0.15,
        output_per_m_usd=0.60,
        cache_hit_per_m_usd=0.075,
        supports_vision=True,
    ),
    "o1": ModelInfo(
        id="o1",
        label="o1 (Preview)",
        input_per_m_usd=15.00,
        output_per_m_usd=60.00,
        cache_hit_per_m_usd=7.50,
    ),
}

OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"in_miss": 2.50, "in_hit": 1.25, "out": 10.00},
    "gpt-4o-mini": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60},
    "o1": {"in_miss": 15.00, "in_hit": 7.50, "out": 60.00},
}

GOOGLE_MODELS: dict[str, ModelInfo] = {
    "gemini-2.0-flash": ModelInfo(
        id="gemini-2.0-flash",
        label="Gemini 2.0 Flash",
        input_per_m_usd=0.10,
        output_per_m_usd=0.40,
        cache_hit_per_m_usd=0.01,
        supports_vision=True,
    ),
    "gemini-1.5-pro": ModelInfo(
        id="gemini-1.5-pro",
        label="Gemini 1.5 Pro",
        input_per_m_usd=1.25,
        output_per_m_usd=5.00,
        cache_hit_per_m_usd=0.3125,
        supports_vision=True,
    ),
    "gemini-1.5-flash": ModelInfo(
        id="gemini-1.5-flash",
        label="Gemini 1.5 Flash",
        input_per_m_usd=0.075,
        output_per_m_usd=0.30,
        cache_hit_per_m_usd=0.01875,
        supports_vision=True,
    ),
    "gemini-1.5-flash-8b": ModelInfo(
        id="gemini-1.5-flash-8b",
        label="Gemini 1.5 Flash-8B",
        input_per_m_usd=0.0375,
        output_per_m_usd=0.15,
        cache_hit_per_m_usd=0.01,
        supports_vision=True,
    ),
    "gemini-2.0-pro-exp-02-05": ModelInfo(
        id="gemini-2.0-pro-exp-02-05",
        label="Gemini 2.0 Pro (Exp)",
        input_per_m_usd=0.00,
        output_per_m_usd=0.00,
        cache_hit_per_m_usd=0.00,
        supports_vision=True,
    ),
}

GOOGLE_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.0-flash": {"in_miss": 0.10, "in_hit": 0.01, "out": 0.40},
    "gemini-1.5-pro": {"in_miss": 1.25, "in_hit": 0.3125, "out": 5.00},
    "gemini-1.5-flash": {"in_miss": 0.075, "in_hit": 0.01875, "out": 0.30},
    "gemini-1.5-flash-8b": {"in_miss": 0.0375, "in_hit": 0.01, "out": 0.15},
    "gemini-2.0-pro-exp-02-05": {"in_miss": 0.00, "in_hit": 0.00, "out": 0.00},
}

ANTHROPIC_MODELS: dict[str, ModelInfo] = {
    "claude-opus-4-7": ModelInfo(
        id="claude-opus-4-7",
        label="Claude Opus 4.7",
        input_per_m_usd=5.00,
        output_per_m_usd=25.00,
        cache_hit_per_m_usd=0.50,
        supports_vision=True,
    ),
    "claude-sonnet-4-6": ModelInfo(
        id="claude-sonnet-4-6",
        label="Claude Sonnet 4.6",
        input_per_m_usd=3.00,
        output_per_m_usd=15.00,
        cache_hit_per_m_usd=0.30,
        supports_vision=True,
    ),
    "claude-haiku-4-5-20251001": ModelInfo(
        id="claude-haiku-4-5-20251001",
        label="Claude Haiku 4.5",
        input_per_m_usd=1.00,
        output_per_m_usd=5.00,
        cache_hit_per_m_usd=0.10,
        supports_vision=True,
    ),
    "claude-sonnet-4-20250514": ModelInfo(
        id="claude-sonnet-4-20250514",
        label="Claude Sonnet 4",
        input_per_m_usd=3.00,
        output_per_m_usd=15.00,
        cache_hit_per_m_usd=0.30,
        supports_vision=True,
    ),
    "claude-3-7-sonnet-20250219": ModelInfo(
        id="claude-3-7-sonnet-20250219",
        label="Claude Sonnet 3.7",
        input_per_m_usd=3.00,
        output_per_m_usd=15.00,
        cache_hit_per_m_usd=0.30,
        supports_vision=True,
    ),
    "claude-3-5-haiku-20241022": ModelInfo(
        id="claude-3-5-haiku-20241022",
        label="Claude Haiku 3.5",
        input_per_m_usd=0.80,
        output_per_m_usd=4.00,
        cache_hit_per_m_usd=0.08,
        supports_vision=True,
    ),
}

ANTHROPIC_PRICING: dict[str, dict[str, float]] = {
    mid: {
        "in_miss": model.input_per_m_usd,
        "in_hit": model.cache_hit_per_m_usd,
        "out": model.output_per_m_usd,
    }
    for mid, model in ANTHROPIC_MODELS.items()
}

OPENROUTER_MODELS: dict[str, ModelInfo] = {
    "openai/gpt-4o": ModelInfo(
        id="openai/gpt-4o",
        label="GPT-4o",
        input_per_m_usd=2.50,
        output_per_m_usd=10.00,
        cache_hit_per_m_usd=1.25,
        supports_vision=True,
    ),
    "anthropic/claude-3.5-sonnet": ModelInfo(
        id="anthropic/claude-3.5-sonnet",
        label="Claude 3.5 Sonnet",
        input_per_m_usd=3.00,
        output_per_m_usd=15.00,
        cache_hit_per_m_usd=0.30,
        supports_vision=True,
    ),
    "anthropic/claude-3.7-sonnet": ModelInfo(
        id="anthropic/claude-3.7-sonnet",
        label="Claude 3.7 Sonnet",
        input_per_m_usd=3.00,
        output_per_m_usd=15.00,
        cache_hit_per_m_usd=0.30,
        supports_vision=True,
    ),
    "google/gemini-2.0-flash-001": ModelInfo(
        id="google/gemini-2.0-flash-001",
        label="Gemini 2.0 Flash",
        input_per_m_usd=0.10,
        output_per_m_usd=0.40,
        cache_hit_per_m_usd=0.01,
        supports_vision=True,
    ),
    "deepseek/deepseek-r1": ModelInfo(
        id="deepseek/deepseek-r1",
        label="DeepSeek R1",
        input_per_m_usd=0.14,
        output_per_m_usd=2.19,
        cache_hit_per_m_usd=0.014,
    ),
}

OPENROUTER_PRICING: dict[str, dict[str, float]] = {
    "openai/gpt-4o": {"in_miss": 2.50, "in_hit": 1.25, "out": 10.00},
    "anthropic/claude-3.5-sonnet": {"in_miss": 3.00, "in_hit": 0.30, "out": 15.00},
    "anthropic/claude-3.7-sonnet": {"in_miss": 3.00, "in_hit": 0.30, "out": 15.00},
    "google/gemini-2.0-flash-001": {"in_miss": 0.10, "in_hit": 0.01, "out": 0.40},
    "deepseek/deepseek-r1": {"in_miss": 0.55, "in_hit": 0.07, "out": 2.19},
}

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

DEFAULT_MODEL: str = PROVIDERS["deepseek"].default_model
DEFAULT_THINKING: ThinkingMode = PROVIDERS["deepseek"].default_thinking
DEFAULT_PLANNER_MODEL: str = "deepseek-v4-flash"
DEFAULT_WORKER_MODEL: str = "deepseek-v4-pro"
DEFAULT_PLANNER_THINKING: ThinkingMode = "off"
DEFAULT_WORKER_THINKING: ThinkingMode = "high"
