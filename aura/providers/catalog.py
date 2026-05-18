"""Provider catalog — plain dicts with mutable model/pricing references.

The module-level ``DEEPSEEK_MODELS``, ``OPENAI_MODELS``, etc. are empty
dicts that the dynamic catalog loader (``load_dynamic_catalog``) populates
at runtime.  Because ``ProviderSpec`` objects share references to these
same dicts, any mutation propagates everywhere.
"""

from __future__ import annotations

from aura.providers.base import ModelInfo, ThinkingMode

# ---------------------------------------------------------------------------
# Mutable model / pricing caches — shared references
# ---------------------------------------------------------------------------

DEEPSEEK_MODELS: dict[str, ModelInfo] = {
    "deepseek-v4-flash": ModelInfo(
        id="deepseek-v4-flash",
        label="deepseek-v4-flash",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "deepseek-v4-pro": ModelInfo(
        id="deepseek-v4-pro",
        label="deepseek-v4-pro",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
DEEPSEEK_PRICING: dict[str, dict[str, float]] = {}

OPENAI_MODELS: dict[str, ModelInfo] = {
    "gpt-4o": ModelInfo(
        id="gpt-4o",
        label="gpt-4o",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
OPENAI_PRICING: dict[str, dict[str, float]] = {}

ANTHROPIC_MODELS: dict[str, ModelInfo] = {
    "claude-sonnet-4-6": ModelInfo(
        id="claude-sonnet-4-6",
        label="claude-sonnet-4-6",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
ANTHROPIC_PRICING: dict[str, dict[str, float]] = {}

OPENROUTER_MODELS: dict[str, ModelInfo] = {
    "openai/gpt-4o": ModelInfo(
        id="openai/gpt-4o",
        label="openai/gpt-4o",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
OPENROUTER_PRICING: dict[str, dict[str, float]] = {}

# ---------------------------------------------------------------------------
# Provider catalogue — raw dict form consumed by ProviderRegistry
# ---------------------------------------------------------------------------

PROVIDER_CATALOG: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-v4-flash",
        "default_thinking": "high",
        "models": DEEPSEEK_MODELS,
        "pricing": DEEPSEEK_PRICING,
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "default_thinking": "off",
        "models": OPENAI_MODELS,
        "pricing": OPENAI_PRICING,
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "openai/gpt-4o",
        "default_thinking": "off",
        "models": OPENROUTER_MODELS,
        "pricing": OPENROUTER_PRICING,
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "default_thinking": "high",
        "models": ANTHROPIC_MODELS,
        "pricing": ANTHROPIC_PRICING,
    },
}

# ---------------------------------------------------------------------------
# Default model / thinking constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL: str = "deepseek-v4-flash"
DEFAULT_THINKING: ThinkingMode = "high"
DEFAULT_PLANNER_MODEL: str = "deepseek-v4-flash"
DEFAULT_WORKER_MODEL: str = "deepseek-v4-pro"
DEFAULT_PLANNER_THINKING: ThinkingMode = "off"
DEFAULT_WORKER_THINKING: ThinkingMode = "high"

# ---------------------------------------------------------------------------
# Google Cloud / Vertex AI — conditional registration
# ---------------------------------------------------------------------------

import os as _os

_AURA_GOOGLE_CLOUD_FLAG = _os.environ.get("AURA_GOOGLE_CLOUD_PROVIDER") == "1"
if _AURA_GOOGLE_CLOUD_FLAG:
    GOOGLE_CLOUD_MODELS: dict[str, ModelInfo] = {
        "gemini-2.0-flash-001": ModelInfo(
            id="gemini-2.0-flash-001",
            label="Gemini 2.0 Flash",
            input_per_m_usd=0.0,
            output_per_m_usd=0.0,
            cache_hit_per_m_usd=0.0,
        ),
        "gemini-2.5-flash-001": ModelInfo(
            id="gemini-2.5-flash-001",
            label="Gemini 2.5 Flash",
            input_per_m_usd=0.0,
            output_per_m_usd=0.0,
            cache_hit_per_m_usd=0.0,
        ),
        "gemini-2.5-pro-001": ModelInfo(
            id="gemini-2.5-pro-001",
            label="Gemini 2.5 Pro",
            input_per_m_usd=0.0,
            output_per_m_usd=0.0,
            cache_hit_per_m_usd=0.0,
        ),
    }
    GOOGLE_CLOUD_PRICING: dict[str, dict[str, float]] = {}

    PROVIDER_CATALOG["google_cloud"] = {
        "label": "Google Cloud Gemini",
        "base_url": "",
        "env_key": "GOOGLE_CLOUD_PROJECT",
        "default_model": "gemini-2.0-flash-001",
        "default_thinking": "off",
        "models": GOOGLE_CLOUD_MODELS,
        "pricing": GOOGLE_CLOUD_PRICING,
    }
