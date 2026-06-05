"""Provider catalog — plain dicts with mutable model/pricing references.

The module-level ``DEEPSEEK_MODELS``, ``OPENAI_MODELS``, etc. are empty
dicts that the dynamic catalog loader (``load_dynamic_catalog``) populates
at runtime.  Because ``ProviderSpec`` objects share references to these
same dicts, any mutation propagates everywhere.
"""

from __future__ import annotations

from aura.providers.base import ModelInfo, ThinkingMode

# Mutable model / pricing caches — shared references

DEEPSEEK_MODELS: dict[str, ModelInfo] = {
    "deepseek-v4-flash": ModelInfo(
        id="deepseek-v4-flash",
        label="DeepSeek V4 Flash",
        input_per_m_usd=0.14,
        output_per_m_usd=0.28,
        cache_hit_per_m_usd=0.0028,
    ),
    "deepseek-v4-pro": ModelInfo(
        id="deepseek-v4-pro",
        label="DeepSeek V4 Pro",
        input_per_m_usd=0.435,
        output_per_m_usd=0.87,
        cache_hit_per_m_usd=0.003625,
    ),
}
DEEPSEEK_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"in_miss": 0.14, "in_hit": 0.0028, "out": 0.28},
    "deepseek-v4-pro": {"in_miss": 0.435, "in_hit": 0.003625, "out": 0.87},
}

OPENAI_MODELS: dict[str, ModelInfo] = {
    "gpt-5.5": ModelInfo(
        id="gpt-5.5",
        label="GPT-5.5",
        input_per_m_usd=10.00,
        output_per_m_usd=40.00,
        cache_hit_per_m_usd=5.00,
    ),
    "gpt-5.4": ModelInfo(
        id="gpt-5.4",
        label="GPT-5.4",
        input_per_m_usd=2.50,
        output_per_m_usd=10.00,
        cache_hit_per_m_usd=1.25,
    ),
    "gpt-5.4-mini": ModelInfo(
        id="gpt-5.4-mini",
        label="GPT-5.4 Mini",
        input_per_m_usd=0.15,
        output_per_m_usd=0.60,
        cache_hit_per_m_usd=0.075,
    ),
    "gpt-5.4-nano": ModelInfo(
        id="gpt-5.4-nano",
        label="GPT-5.4 Nano",
        input_per_m_usd=0.10,
        output_per_m_usd=0.40,
        cache_hit_per_m_usd=0.05,
    ),
}
OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5": {"in_miss": 10.00, "in_hit": 5.00, "out": 40.00},
    "gpt-5.4": {"in_miss": 2.50, "in_hit": 1.25, "out": 10.00},
    "gpt-5.4-mini": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60},
    "gpt-5.4-nano": {"in_miss": 0.10, "in_hit": 0.05, "out": 0.40},
}

ANTHROPIC_MODELS: dict[str, ModelInfo] = {
    "claude-opus-4-7": ModelInfo(
        id="claude-opus-4-7",
        label="Claude Opus 4.7",
        input_per_m_usd=15.00,
        output_per_m_usd=75.00,
        cache_hit_per_m_usd=1.50,
    ),
    "claude-sonnet-4-6": ModelInfo(
        id="claude-sonnet-4-6",
        label="Claude Sonnet 4.6",
        input_per_m_usd=3.00,
        output_per_m_usd=15.00,
        cache_hit_per_m_usd=0.30,
    ),
    "claude-haiku-4-5": ModelInfo(
        id="claude-haiku-4-5",
        label="Claude Haiku 4.5",
        input_per_m_usd=0.25,
        output_per_m_usd=1.25,
        cache_hit_per_m_usd=0.025,
    ),
    "claude-haiku-4-5-20251001": ModelInfo(
        id="claude-haiku-4-5-20251001",
        label="Claude Haiku 4.5 Snapshot",
        input_per_m_usd=0.25,
        output_per_m_usd=1.25,
        cache_hit_per_m_usd=0.025,
    ),
}
ANTHROPIC_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {"in_miss": 15.00, "in_hit": 1.50, "out": 75.00},
    "claude-sonnet-4-6": {"in_miss": 3.00, "in_hit": 0.30, "out": 15.00},
    "claude-haiku-4-5": {"in_miss": 0.25, "in_hit": 0.025, "out": 1.25},
    "claude-haiku-4-5-20251001": {"in_miss": 0.25, "in_hit": 0.025, "out": 1.25},
}

OPENROUTER_MODELS: dict[str, ModelInfo] = {
    "deepseek/deepseek-v4-flash": ModelInfo(
        id="deepseek/deepseek-v4-flash",
        label="DeepSeek V4 Flash",
        input_per_m_usd=0.15,
        output_per_m_usd=0.60,
        cache_hit_per_m_usd=0.075,
    ),
    "openai/gpt-oss-120b": ModelInfo(
        id="openai/gpt-oss-120b",
        label="OpenAI GPT-OSS 120B",
        input_per_m_usd=2.00,
        output_per_m_usd=8.00,
        cache_hit_per_m_usd=1.00,
    ),
    "openai/gpt-oss-20b": ModelInfo(
        id="openai/gpt-oss-20b",
        label="OpenAI GPT-OSS 20B",
        input_per_m_usd=0.40,
        output_per_m_usd=1.60,
        cache_hit_per_m_usd=0.20,
    ),
    "qwen/qwen3-coder:free": ModelInfo(
        id="qwen/qwen3-coder:free",
        label="Qwen3 Coder Free",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "meta-llama/llama-3.3-70b-instruct:free": ModelInfo(
        id="meta-llama/llama-3.3-70b-instruct:free",
        label="Llama 3.3 70B Free",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "openrouter/owl-alpha": ModelInfo(
        id="openrouter/owl-alpha",
        label="Owl Alpha",
        input_per_m_usd=2.00,
        output_per_m_usd=8.00,
        cache_hit_per_m_usd=1.00,
    ),
}
OPENROUTER_PRICING: dict[str, dict[str, float]] = {
    "deepseek/deepseek-v4-flash": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60},
    "openai/gpt-oss-120b": {"in_miss": 2.00, "in_hit": 1.00, "out": 8.00},
    "openai/gpt-oss-20b": {"in_miss": 0.40, "in_hit": 0.20, "out": 1.60},
    "qwen/qwen3-coder:free": {"in_miss": 0.0, "in_hit": 0.0, "out": 0.0},
    "meta-llama/llama-3.3-70b-instruct:free": {"in_miss": 0.0, "in_hit": 0.0, "out": 0.0},
    "openrouter/owl-alpha": {"in_miss": 2.00, "in_hit": 1.00, "out": 8.00},
}

# Google Cloud / Vertex AI

GOOGLE_CLOUD_MODELS: dict[str, ModelInfo] = {
    "gemini-2.5-pro": ModelInfo(
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        input_per_m_usd=1.25,
        output_per_m_usd=5.00,
        cache_hit_per_m_usd=0.3125,
    ),
    "gemini-2.5-flash": ModelInfo(
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        input_per_m_usd=0.075,
        output_per_m_usd=0.30,
        cache_hit_per_m_usd=0.01875,
    ),
    "gemini-2.0-flash": ModelInfo(
        id="gemini-2.0-flash",
        label="Gemini 2.0 Flash",
        input_per_m_usd=0.10,
        output_per_m_usd=0.40,
        cache_hit_per_m_usd=0.025,
    ),
    "gemini-1.5-pro": ModelInfo(
        id="gemini-1.5-pro",
        label="Gemini 1.5 Pro",
        input_per_m_usd=1.25,
        output_per_m_usd=5.00,
        cache_hit_per_m_usd=0.3125,
    ),
    "gemini-1.5-flash": ModelInfo(
        id="gemini-1.5-flash",
        label="Gemini 1.5 Flash",
        input_per_m_usd=0.075,
        output_per_m_usd=0.30,
        cache_hit_per_m_usd=0.01875,
    ),
}
GOOGLE_CLOUD_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-pro": {"in_miss": 1.25, "in_hit": 0.3125, "out": 5.00},
    "gemini-2.5-flash": {"in_miss": 0.075, "in_hit": 0.01875, "out": 0.30},
    "gemini-2.0-flash": {"in_miss": 0.10, "in_hit": 0.025, "out": 0.40},
    "gemini-1.5-pro": {"in_miss": 1.25, "in_hit": 0.3125, "out": 5.00},
    "gemini-1.5-flash": {"in_miss": 0.075, "in_hit": 0.01875, "out": 0.30},
}

# Provider catalogue — raw dict form consumed by ProviderRegistry

PROVIDER_CATALOG: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-v4-flash",
        "default_thinking": "high",
        "models": DEEPSEEK_MODELS,
        "pricing": DEEPSEEK_PRICING,
        "kind": "api_key",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-5.4-mini",
        "default_thinking": "off",
        "models": OPENAI_MODELS,
        "pricing": OPENAI_PRICING,
        "kind": "api_key",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "deepseek/deepseek-v4-flash",
        "default_thinking": "off",
        "models": OPENROUTER_MODELS,
        "pricing": OPENROUTER_PRICING,
        "kind": "api_key",
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "default_thinking": "high",
        "models": ANTHROPIC_MODELS,
        "pricing": ANTHROPIC_PRICING,
        "kind": "api_key",
    },
    "google_cloud": {
        "label": "Google Gemini",
        "base_url": "",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash",
        "default_thinking": "off",
        "models": GOOGLE_CLOUD_MODELS,
        "pricing": GOOGLE_CLOUD_PRICING,
        "kind": "api_key",
    },
    "claude_code": {
        "label": "Claude Code",
        "base_url": "",
        "env_key": "",
        "default_model": "claude-code",
        "default_thinking": "off",
        "models": {
            "claude-code": ModelInfo(
                id="claude-code",
                label="Claude Code Agent",
                input_per_m_usd=0.0,
                output_per_m_usd=0.0,
                cache_hit_per_m_usd=0.0,
            )
        },
        "pricing": {},
        "kind": "external_cli",
    },
    "codex": {
        "label": "Codex",
        "base_url": "",
        "env_key": "",
        "default_model": "codex",
        "default_thinking": "off",
        "models": {
            "codex": ModelInfo(
                id="codex",
                label="Codex Agent",
                input_per_m_usd=0.0,
                output_per_m_usd=0.0,
                cache_hit_per_m_usd=0.0,
            )
        },
        "pricing": {},
        "kind": "external_cli",
    },
}

# Default model / thinking constants

DEFAULT_MODEL: str = "deepseek-v4-flash"
DEFAULT_THINKING: ThinkingMode = "high"
DEFAULT_PLANNER_MODEL: str = "deepseek-v4-flash"
DEFAULT_WORKER_MODEL: str = "deepseek-v4-pro"
DEFAULT_PLANNER_THINKING: ThinkingMode = "off"
DEFAULT_WORKER_THINKING: ThinkingMode = "high"
