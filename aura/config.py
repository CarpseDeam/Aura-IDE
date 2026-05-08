"""Settings, paths, model registry, and pricing constants for Aura."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "Aura"
APP_AUTHOR = "Aura"

# ---------------------------------------------------------------------------
# Provider types and registry
# ---------------------------------------------------------------------------

ProviderId = Literal["deepseek", "openai", "google", "openrouter"]
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
    "gpt-4.1": ModelInfo(
        id="gpt-4.1",
        label="GPT-4.1",
        input_per_m_usd=2.00,
        output_per_m_usd=8.00,
        cache_hit_per_m_usd=0.50,
        supports_vision=True,
    ),
}

OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"in_miss": 2.50, "in_hit": 1.25, "out": 10.00},
    "gpt-4o-mini": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60},
    "gpt-4.1": {"in_miss": 2.00, "in_hit": 0.50, "out": 8.00},
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
    "gemini-2.0-pro-exp-02-05": ModelInfo(
        id="gemini-2.0-pro-exp-02-05",
        label="Gemini 2.0 Pro (Exp)",
        input_per_m_usd=0.00,
        output_per_m_usd=0.00,
        cache_hit_per_m_usd=0.00,
        supports_vision=True,
    ),
    "gemini-1.5-pro": ModelInfo(
        id="gemini-1.5-pro",
        label="Gemini 1.5 Pro",
        input_per_m_usd=1.25,
        output_per_m_usd=10.00,
        cache_hit_per_m_usd=0.25,
        supports_vision=True,
    ),
}

GOOGLE_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.0-flash": {"in_miss": 0.10, "in_hit": 0.01, "out": 0.40},
    "gemini-2.0-pro-exp-02-05": {"in_miss": 0.00, "in_hit": 0.00, "out": 0.00},
    "gemini-1.5-pro": {"in_miss": 1.25, "in_hit": 0.25, "out": 10.00},
}

OPENROUTER_MODELS: dict[str, ModelInfo] = {
    "openai/gpt-4o": ModelInfo(
        id="openai/gpt-4o",
        label="OpenAI GPT-4o",
        input_per_m_usd=2.50,
        output_per_m_usd=10.00,
        cache_hit_per_m_usd=1.25,
        supports_vision=True,
    ),
    "openai/gpt-4o-mini": ModelInfo(
        id="openai/gpt-4o-mini",
        label="OpenAI GPT-4o Mini",
        input_per_m_usd=0.15,
        output_per_m_usd=0.60,
        cache_hit_per_m_usd=0.075,
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
    "google/gemini-2.0-pro-exp-02-05:free": ModelInfo(
        id="google/gemini-2.0-pro-exp-02-05:free",
        label="Gemini 2.0 Pro (Free)",
        input_per_m_usd=0.00,
        output_per_m_usd=0.00,
        cache_hit_per_m_usd=0.00,
        supports_vision=True,
    ),
    "meta-llama/llama-3.1-8b-instruct": ModelInfo(
        id="meta-llama/llama-3.1-8b-instruct",
        label="Llama 3.1 8B",
        input_per_m_usd=0.06,
        output_per_m_usd=0.06,
        cache_hit_per_m_usd=0.03,
    ),
    "x-ai/grok-2-1212": ModelInfo(
        id="x-ai/grok-2-1212",
        label="Grok 2",
        input_per_m_usd=2.00,
        output_per_m_usd=10.00,
        cache_hit_per_m_usd=1.00,
        supports_vision=True,
    ),
}

OPENROUTER_PRICING: dict[str, dict[str, float]] = {
    "openai/gpt-4o": {"in_miss": 2.50, "in_hit": 1.25, "out": 10.00},
    "openai/gpt-4o-mini": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60},
    "anthropic/claude-3.5-sonnet": {"in_miss": 3.00, "in_hit": 0.30, "out": 15.00},
    "anthropic/claude-3.7-sonnet": {"in_miss": 3.00, "in_hit": 0.30, "out": 15.00},
    "meta-llama/llama-3.1-8b-instruct": {"in_miss": 0.06, "in_hit": 0.03, "out": 0.06},
    "meta-llama/llama-3.1-70b-instruct": {"in_miss": 0.59, "in_hit": 0.30, "out": 0.79},
    "mistralai/mistral-7b-instruct": {"in_miss": 0.06, "in_hit": 0.03, "out": 0.06},
    "x-ai/grok-2-1212": {"in_miss": 2.00, "in_hit": 1.00, "out": 10.00},
    "google/gemini-2.5-flash-001": {"in_miss": 0.15, "in_hit": 0.015, "out": 0.60},
    "google/gemini-2.5-pro-001": {"in_miss": 1.25, "in_hit": 0.25, "out": 10.00},
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
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        env_key="GEMINI_API_KEY",
        default_model="gemini-2.5-flash",
        default_thinking="off",
        models=GOOGLE_MODELS,
        pricing=GOOGLE_PRICING,
    ),
    "openrouter": ProviderConfig(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
        default_thinking="off",
        models=OPENROUTER_MODELS,
        pricing=OPENROUTER_PRICING,
    ),
}

# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

DEEPSEEK_BASE_URL: str = PROVIDERS["deepseek"].base_url
ENV_API_KEY: str = PROVIDERS["deepseek"].env_key
DEFAULT_PROVIDER: ProviderId = "deepseek"

# Deprecated module-level model dict — kept for backward compat with smoke scripts.
MODELS: dict[str, ModelInfo] = dict(PROVIDERS["deepseek"].models)

# ---------------------------------------------------------------------------
# Tavily (web search) API key
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY_ENV: str = "OPENROUTER_API_KEY"

TAVILY_API_KEY_ENV: str = "TAVILY_API_KEY"

# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------


def get_provider(provider_id: ProviderId) -> ProviderConfig:
    return PROVIDERS[provider_id]


def get_api_key(provider_id: ProviderId) -> str | None:
    """Check env var only — API keys are never stored in config.json."""
    cfg = PROVIDERS[provider_id]
    return os.environ.get(cfg.env_key) or None


def resolve_api_key(provider_id: ProviderId) -> str:
    """Like require_api_key but provider-aware. Raises RuntimeError if not found."""
    key = get_api_key(provider_id)
    if not key:
        cfg = PROVIDERS[provider_id]
        raise RuntimeError(
            f"No API key found for {cfg.label}. "
            f"Set the {cfg.env_key} environment variable."
        )
    return key


def has_api_key(provider_id: ProviderId | None = None) -> bool:
    """If provider_id is None, checks the default provider (deepseek)."""
    pid = provider_id if provider_id is not None else DEFAULT_PROVIDER
    return get_api_key(pid) is not None


def require_api_key() -> str:
    """Legacy wrapper — checks the default (DeepSeek) provider."""
    return resolve_api_key(DEFAULT_PROVIDER)


def get_tavily_api_key() -> str | None:
    """Read TAVILY_API_KEY from environment. Returns None if not set."""
    return os.environ.get(TAVILY_API_KEY_ENV) or None


def fetch_provider_models(provider_id: ProviderId) -> tuple[dict[str, ModelInfo], dict[str, dict[str, float]]]:
    """Fetch models and pricing from the provider's API.
    
    Returns (models_dict, pricing_dict).
    """
    from aura.client.deepseek import DeepSeekClient
    
    try:
        client = DeepSeekClient(provider=provider_id)
        raw = client.fetch_raw_models()
    except Exception:
        return {}, {}
        
    models: dict[str, ModelInfo] = {}
    pricing: dict[str, dict[str, float]] = {}
    
    if provider_id == "openrouter":
        for m in raw:
            mid = m.get("id", "")
            if not mid:
                continue
            
            # OpenRouter gives us friendly names and pricing!
            name = m.get("name") or mid
            # Pricing is in USD per 1k tokens in the API, we want per 1M.
            # actually OpenRouter API returns strings for pricing sometimes or floats.
            # Per docs: prompt, completion are in USD.
            p = m.get("pricing", {})
            try:
                # Convert from price-per-token to price-per-1M-tokens
                in_m = float(p.get("prompt", 0)) * 1_000_000
                out_m = float(p.get("completion", 0)) * 1_000_000
                # OpenRouter doesn't always expose cache pricing per-model in this endpoint.
                # We'll default hit to half of miss if unknown.
                hit_m = float(p.get("request", 0)) * 1_000_000 or (in_m * 0.5)
            except (ValueError, TypeError):
                in_m = out_m = hit_m = 0.0

            # Detect vision support from OpenRouter modalities
            modalities = m.get("architecture", {}).get("modalities", [])
            supports_vision = "image" in modalities

            models[mid] = ModelInfo(
                id=mid,
                label=name,
                input_per_m_usd=in_m,
                output_per_m_usd=out_m,
                cache_hit_per_m_usd=hit_m,
                supports_vision=supports_vision
            )
            pricing[mid] = {"in_miss": in_m, "in_hit": hit_m, "out": out_m}
    else:
        # Standard OpenAI-compatible (DeepSeek, Google, OpenAI)
        # These usually just give a list of IDs.
        for m in raw:
            mid = m.get("id") or m.get("name") # Some variants use name
            if not mid:
                continue
            
            # We don't have pricing for dynamic models on these providers 
            # (they don't expose it via API usually). 
            # We'll check if we have hardcoded pricing first.
            existing_p = get_pricing(mid)
            if existing_p:
                in_m = existing_p["in_miss"]
                hit_m = existing_p["in_hit"]
                out_m = existing_p["out"]
            else:
                in_m = out_m = hit_m = 0.0
            
            # Friendly label: capitalized ID or stripped prefix
            label = mid.split("/")[-1].replace("-", " ").title()
            
            # Default vision support check for hardcoded models
            supports_vision = False
            for p_cfg in PROVIDERS.values():
                if mid in p_cfg.models:
                    supports_vision = p_cfg.models[mid].supports_vision
                    break

            models[mid] = ModelInfo(
                id=mid,
                label=label,
                input_per_m_usd=in_m,
                output_per_m_usd=out_m,
                cache_hit_per_m_usd=hit_m,
                supports_vision=supports_vision
            )
            pricing[mid] = {"in_miss": in_m, "in_hit": hit_m, "out": out_m}

    return models, pricing


def require_tavily_api_key() -> str:
    """Like get_tavily_api_key() but raises RuntimeError if not found."""
    key = get_tavily_api_key()
    if not key:
        raise RuntimeError(
            "Tavily API key not found. "
            f"Set the {TAVILY_API_KEY_ENV} environment variable."
        )
    return key


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------

PRICING: dict[str, dict[str, float]] = dict(PROVIDERS["deepseek"].pricing)


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
# Vision defaults (local Ollama model)
# ---------------------------------------------------------------------------

DEFAULT_VISION_ENABLED = True
DEFAULT_VISION_MODEL = "llama3.2-vision"
DEFAULT_VISION_ENDPOINT = "http://localhost:11434/v1"

# ---------------------------------------------------------------------------
# Hard limits
# ---------------------------------------------------------------------------

# Hard limit on tool-call rounds within a single user turn.
MAX_TOOL_ROUNDS = 50
# Cap for read_file and similar to avoid blowing the context window.
MAX_READ_BYTES = 200 * 1024
# Cap on glob results.
MAX_GLOB_RESULTS = 200

# Token budget for the conversation context window. DeepSeek V4 models support
# 64K tokens; we keep headroom for the model's response and misc overhead.
MAX_CONTEXT_TOKENS = 60_000

# When pruning old tool results, keep at most this many characters of the
# original content. Longer results are replaced with a truncation marker.
TRUNCATE_TOOL_RESULT_CHARS = 500

SKIP_DIRS = {"__pycache__", ".venv", ".git", "node_modules", ".import", ".aura"}
SKIP_FILE_SUFFIXES = {".import"}

# ---------------------------------------------------------------------------
# Default model/thinking constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL: str = PROVIDERS["deepseek"].default_model
DEFAULT_THINKING: ThinkingMode = PROVIDERS["deepseek"].default_thinking
DEFAULT_PLANNER_MODEL: str = "deepseek-v4-flash"
DEFAULT_WORKER_MODEL: str = "deepseek-v4-pro"
DEFAULT_PLANNER_THINKING: ThinkingMode = "high"
DEFAULT_WORKER_THINKING: ThinkingMode = "high"


# ---------------------------------------------------------------------------
# App settings (persisted JSON)
# ---------------------------------------------------------------------------


@dataclass
class AppSettings:
    provider: ProviderId = DEFAULT_PROVIDER
    default_model: str = DEFAULT_MODEL
    default_thinking: ThinkingMode = DEFAULT_THINKING
    restore_last_conversation: bool = True
    planner_worker_mode: bool = True
    default_planner_model: str = DEFAULT_PLANNER_MODEL
    default_worker_model: str = DEFAULT_WORKER_MODEL
    default_planner_thinking: ThinkingMode = DEFAULT_PLANNER_THINKING
    default_worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
    vision_enabled: bool = DEFAULT_VISION_ENABLED
    vision_model: str = DEFAULT_VISION_MODEL
    vision_endpoint: str = DEFAULT_VISION_ENDPOINT
    temperature: float = 0.7
    worker_temperature: float = 0.1
    system_prompt: str = ""
    planner_system_prompt: str = ""
    worker_system_prompt: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        s = cls()
        # Provider
        if isinstance(data.get("provider"), str) and data["provider"] in PROVIDERS:
            s.provider = data["provider"]  # type: ignore[assignment]
        # Models — accept any string now
        if isinstance(data.get("default_model"), str):
            s.default_model = data["default_model"]
        if isinstance(data.get("default_thinking"), str) and data["default_thinking"] in ("off", "high", "max"):
            s.default_thinking = data["default_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("restore_last_conversation"), bool):
            s.restore_last_conversation = data["restore_last_conversation"]
        if isinstance(data.get("planner_worker_mode"), bool):
            s.planner_worker_mode = data["planner_worker_mode"]
        if isinstance(data.get("default_planner_model"), str):
            s.default_planner_model = data["default_planner_model"]
        if isinstance(data.get("default_worker_model"), str):
            s.default_worker_model = data["default_worker_model"]
        if isinstance(data.get("default_planner_thinking"), str) and data["default_planner_thinking"] in ("off", "high", "max"):
            s.default_planner_thinking = data["default_planner_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("default_worker_thinking"), str) and data["default_worker_thinking"] in ("off", "high", "max"):
            s.default_worker_thinking = data["default_worker_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("vision_enabled"), bool):
            s.vision_enabled = data["vision_enabled"]
        if isinstance(data.get("vision_model"), str):
            s.vision_model = data["vision_model"]
        if isinstance(data.get("vision_endpoint"), str):
            s.vision_endpoint = data["vision_endpoint"]
        # Temperature
        if "temperature" in data:
            raw = data["temperature"]
            if isinstance(raw, (int, float)):
                s.temperature = max(0.0, min(2.0, float(raw)))
        if "worker_temperature" in data:
            raw = data["worker_temperature"]
            if isinstance(raw, (int, float)):
                s.worker_temperature = max(0.0, min(2.0, float(raw)))
        # System prompts
        if isinstance(data.get("system_prompt"), str):
            s.system_prompt = data["system_prompt"]
        if isinstance(data.get("planner_system_prompt"), str):
            s.planner_system_prompt = data["planner_system_prompt"]
        if isinstance(data.get("worker_system_prompt"), str):
            s.worker_system_prompt = data["worker_system_prompt"]
        return s


# ---------------------------------------------------------------------------
# Paths and file helpers
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    p = Path(user_config_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    p = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_root_pointer() -> Path:
    """File that stores the last-selected workspace root path."""
    return config_dir() / "workspace_root.txt"


def load_workspace_root() -> Path | None:
    p = workspace_root_pointer()
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_dir() else None


def save_workspace_root(path: Path) -> None:
    workspace_root_pointer().write_text(str(path.resolve()), encoding="utf-8")


def icon_path() -> Path:
    """Return the absolute path to the application window icon (AurA.ico)."""
    return Path(__file__).resolve().parent / "icon.ico"


def media_path(name: str) -> Path:
    """Return the absolute path to a file in the project media/ directory."""
    return Path(__file__).resolve().parent.parent / "media" / name


# ---- settings persistence -------------------------------------------------


def settings_path() -> Path:
    return config_dir() / "config.json"


def load_settings() -> AppSettings:
    p = settings_path()
    if not p.exists():
        return AppSettings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    if not isinstance(data, dict):
        return AppSettings()
    return AppSettings.from_dict(data)


def save_settings(settings: AppSettings) -> None:
    p = settings_path()
    p.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


# ---- dynamic catalog persistence ------------------------------------------

def catalog_cache_path() -> Path:
    return config_dir() / "models_cache.json"


def save_dynamic_catalog(provider_id: ProviderId, models: dict[str, ModelInfo], pricing: dict[str, dict[str, float]]) -> None:
    """Save dynamically fetched models to a local cache file."""
    path = catalog_cache_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    
    # Convert ModelInfo to dict for JSON
    models_raw = {k: asdict(v) for k, v in models.items()}
    data[provider_id] = {
        "models": models_raw,
        "pricing": pricing
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_dynamic_catalog() -> None:
    """Load cached models and update the PROVIDERS global registry."""
    path = catalog_cache_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    for pid, entry in data.items():
        if pid not in PROVIDERS:
            continue
        cfg = PROVIDERS[pid] # type: ignore[literal-required]
        
        cached_models = entry.get("models", {})
        cached_pricing = entry.get("pricing", {})
        
        # Merge cached models into the provider's model dict
        for mid, m_data in cached_models.items():
            cfg.models[mid] = ModelInfo(**m_data)
        # Merge cached pricing into the provider's pricing dict
        for mid, p_data in cached_pricing.items():
            cfg.pricing[mid] = p_data

# Restore dynamic models on load
load_dynamic_catalog()
