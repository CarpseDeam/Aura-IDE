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
        label="DeepSeek V4 Flash",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "deepseek-v4-pro": ModelInfo(
        id="deepseek-v4-pro",
        label="DeepSeek V4 Pro",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
DEEPSEEK_PRICING: dict[str, dict[str, float]] = {}

OPENAI_MODELS: dict[str, ModelInfo] = {
    "gpt-5.5": ModelInfo(
        id="gpt-5.5",
        label="GPT-5.5",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gpt-5.4": ModelInfo(
        id="gpt-5.4",
        label="GPT-5.4",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gpt-5.4-mini": ModelInfo(
        id="gpt-5.4-mini",
        label="GPT-5.4 Mini",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gpt-5.4-nano": ModelInfo(
        id="gpt-5.4-nano",
        label="GPT-5.4 Nano",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
OPENAI_PRICING: dict[str, dict[str, float]] = {}

ANTHROPIC_MODELS: dict[str, ModelInfo] = {
    "claude-opus-4-7": ModelInfo(
        id="claude-opus-4-7",
        label="Claude Opus 4.7",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "claude-sonnet-4-6": ModelInfo(
        id="claude-sonnet-4-6",
        label="Claude Sonnet 4.6",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "claude-haiku-4-5": ModelInfo(
        id="claude-haiku-4-5",
        label="Claude Haiku 4.5",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "claude-haiku-4-5-20251001": ModelInfo(
        id="claude-haiku-4-5-20251001",
        label="Claude Haiku 4.5 Snapshot",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
ANTHROPIC_PRICING: dict[str, dict[str, float]] = {}

OPENROUTER_MODELS: dict[str, ModelInfo] = {
    "deepseek/deepseek-v4-flash": ModelInfo(
        id="deepseek/deepseek-v4-flash",
        label="DeepSeek V4 Flash",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "openai/gpt-oss-120b": ModelInfo(
        id="openai/gpt-oss-120b",
        label="OpenAI GPT-OSS 120B",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "openai/gpt-oss-20b": ModelInfo(
        id="openai/gpt-oss-20b",
        label="OpenAI GPT-OSS 20B",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
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
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
OPENROUTER_PRICING: dict[str, dict[str, float]] = {}

# ---------------------------------------------------------------------------
# Google Cloud / Vertex AI
# ---------------------------------------------------------------------------

GOOGLE_CLOUD_MODELS: dict[str, ModelInfo] = {
    "gemini-3.1-pro-preview": ModelInfo(
        id="gemini-3.1-pro-preview",
        label="Gemini 3.1 Pro Preview",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gemini-3-flash-preview": ModelInfo(
        id="gemini-3-flash-preview",
        label="Gemini 3 Flash Preview",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gemini-3.1-flash-lite": ModelInfo(
        id="gemini-3.1-flash-lite",
        label="Gemini 3.1 Flash-Lite",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gemini-2.5-pro": ModelInfo(
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gemini-2.5-flash": ModelInfo(
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
    "gemini-2.5-flash-lite": ModelInfo(
        id="gemini-2.5-flash-lite",
        label="Gemini 2.5 Flash-Lite",
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    ),
}
GOOGLE_CLOUD_PRICING: dict[str, dict[str, float]] = {}

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
        "default_model": "gpt-5.4-mini",
        "default_thinking": "off",
        "models": OPENAI_MODELS,
        "pricing": OPENAI_PRICING,
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "deepseek/deepseek-v4-flash",
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
    "google_cloud": {
        "label": "Google Cloud Gemini",
        "base_url": "",
        "env_key": "GOOGLE_CLOUD_PROJECT",
        "default_model": "gemini-2.5-flash",
        "default_thinking": "off",
        "models": GOOGLE_CLOUD_MODELS,
        "pricing": GOOGLE_CLOUD_PRICING,
    },
    "gemini_cli": {
        "label": "Gemini CLI",
        "base_url": "",
        "env_key": "",
        "default_model": "gemini-cli",
        "default_thinking": "off",
        "models": {
            "gemini-cli": ModelInfo(
                id="gemini-cli",
                label="Gemini CLI Agent",
                input_per_m_usd=0.0,
                output_per_m_usd=0.0,
                cache_hit_per_m_usd=0.0,
            )
        },
        "pricing": {},
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
