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

ProviderId = Literal["deepseek", "openai", "google"]
ThinkingMode = Literal["off", "high", "max"]
ModelId = str  # Any model string from any provider


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    input_per_m_usd: float
    output_per_m_usd: float
    cache_hit_per_m_usd: float


@dataclass(frozen=True)
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
    ),
    "gpt-4o-mini": ModelInfo(
        id="gpt-4o-mini",
        label="GPT-4o Mini",
        input_per_m_usd=0.15,
        output_per_m_usd=0.60,
        cache_hit_per_m_usd=0.075,
    ),
    "gpt-4.1": ModelInfo(
        id="gpt-4.1",
        label="GPT-4.1",
        input_per_m_usd=2.00,
        output_per_m_usd=8.00,
        cache_hit_per_m_usd=0.50,
    ),
}

OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"in_miss": 2.50, "in_hit": 1.25, "out": 10.00},
    "gpt-4o-mini": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60},
    "gpt-4.1": {"in_miss": 2.00, "in_hit": 0.50, "out": 8.00},
}

GOOGLE_MODELS: dict[str, ModelInfo] = {
    "gemini-2.5-flash": ModelInfo(
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        input_per_m_usd=0.15,
        output_per_m_usd=0.60,
        cache_hit_per_m_usd=0.015,
    ),
    "gemini-2.5-pro": ModelInfo(
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        input_per_m_usd=1.25,
        output_per_m_usd=10.00,
        cache_hit_per_m_usd=0.25,
    ),
}

GOOGLE_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {"in_miss": 0.15, "in_hit": 0.015, "out": 0.60},
    "gemini-2.5-pro": {"in_miss": 1.25, "in_hit": 0.25, "out": 10.00},
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
