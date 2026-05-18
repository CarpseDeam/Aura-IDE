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

DEEPSEEK_MODELS: dict[str, ModelInfo] = {}
DEEPSEEK_PRICING: dict[str, dict[str, float]] = {}

OPENAI_MODELS: dict[str, ModelInfo] = {}
OPENAI_PRICING: dict[str, dict[str, float]] = {}

ANTHROPIC_MODELS: dict[str, ModelInfo] = {}
ANTHROPIC_PRICING: dict[str, dict[str, float]] = {}

OPENROUTER_MODELS: dict[str, ModelInfo] = {}
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
