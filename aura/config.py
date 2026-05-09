"""Settings, paths, model registry, and pricing constants for Aura."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Any

from aura.paths import APP_NAME, APP_AUTHOR, config_dir, data_dir
from aura.key_manager import get_key as _stored_get_key, has_key as _stored_has_key, set_key as _stored_set_key

# ---------------------------------------------------------------------------
# Provider types and registry
# ---------------------------------------------------------------------------

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
    "gemini-2.0-pro-exp-02-05": {"in_miss": 0.00, "in_hit": 0.00, "out": 0.00},
}

ANTHROPIC_MODELS: dict[str, ModelInfo] = {}

ANTHROPIC_PRICING: dict[str, dict[str, float]] = {}

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
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
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
        default_model="claude-sonnet-4-20250514",
        default_thinking="high",
        models=ANTHROPIC_MODELS,
        pricing=ANTHROPIC_PRICING,
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
    """Check env var first, then hardware-encrypted stored key."""
    cfg = PROVIDERS[provider_id]
    # 1. Environment variable takes precedence
    env_val = os.environ.get(cfg.env_key)
    if env_val:
        return env_val
    # 2. Stored key (hardware-tethered, auto-migrates legacy plaintext)
    return _stored_get_key(provider_id)


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


def set_api_key(provider_id: ProviderId, api_key: str) -> None:
    """Store an API key encrypted with a hardware-derived key."""
    _stored_set_key(provider_id, api_key)


def list_stored_providers() -> list[ProviderId]:
    """Return list of provider IDs that have a stored key (encrypted or legacy)."""
    result: list[ProviderId] = []
    for pid in PROVIDERS:
        if _stored_has_key(pid):
            result.append(pid)
    return result


def get_tavily_api_key() -> str | None:
    """Read the Tavily key from the environment or saved app settings."""
    env_key = os.environ.get(TAVILY_API_KEY_ENV)
    if env_key:
        return env_key
    try:
        return load_settings().tavily_api_key or None
    except Exception:
        return None


def fetch_provider_models(provider_id: ProviderId) -> tuple[dict[str, ModelInfo], dict[str, dict[str, float]], str | None]:
    """Fetch models and pricing from the provider's API.
    
    Returns (models_dict, pricing_dict, error_message).
    """
    from aura.client.deepseek import DeepSeekClient
    
    try:
        client = DeepSeekClient(provider=provider_id)
        raw = client.fetch_raw_models()
        if not raw:
            return {}, {}, "Provider returned no models. Check your API key or connection."
    except Exception as exc:
        return {}, {}, str(exc)
        
    models: dict[str, ModelInfo] = {}
    pricing: dict[str, dict[str, float]] = {}
    
    if provider_id == "openrouter":
        for m in raw:
            mid = m.get("id", "")
            if not mid:
                continue
            
            # OpenRouter gives us friendly names and pricing!
            name = m.get("name") or mid
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
        for m in raw:
            mid = m.get("id") or m.get("name")
            if not mid:
                continue
            
            existing_p = get_pricing(mid)
            if existing_p:
                in_m = existing_p["in_miss"]
                hit_m = existing_p["in_hit"]
                out_m = existing_p["out"]
            else:
                in_m = out_m = hit_m = 0.0
            
            label = mid.split("/")[-1].replace("-", " ").title()
            
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

    return models, pricing, None


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

# ---------------------------------------------------------------------------
# Sandbox configuration
# ---------------------------------------------------------------------------

DEFAULT_SANDBOX_MODE: str = "host"
"""Default sandbox mode for terminal commands and dynamic tools.
'host' — run directly on the host (no isolation).
'docker' — run inside a Docker container with resource limits.
'wasm' — reserved for future WASM runtime.
"""

# Token budget for the conversation context window. DeepSeek V4 models support
# 64K tokens; we keep headroom for the model's response and misc overhead.
MAX_CONTEXT_TOKENS = 60_000

# When pruning old tool results, keep at most this many characters of the
# original content. Longer results are replaced with a truncation marker.
TRUNCATE_TOOL_RESULT_CHARS = 500

SKIP_DIRS = {"__pycache__", ".venv", ".git", "node_modules", ".import", ".aura"}
SKIP_FILE_SUFFIXES = {".import"}

# ---------------------------------------------------------------------------
# Codebase index (BM25 search_codebase tool)
# ---------------------------------------------------------------------------

# File extensions to include when building the codebase index.
# Must be lowercase, with leading dot.
CODEBASE_INDEX_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".gd", ".cpp", ".c", ".h", ".hpp",
    ".rs", ".go", ".java", ".rb", ".php", ".swift", ".kt", ".scala",
    ".cfg", ".toml", ".yaml", ".yml", ".json", ".xml", ".ini",
    ".md", ".rst", ".txt", ".sh", ".bash", ".zsh", ".fish",
    ".css", ".scss", ".less", ".html", ".vue", ".svelte",
    ".sql", ".r", ".lua", ".zig", ".odin",
}

# Maximum number of files to index. Beyond this, the indexer stops.
MAX_CODEBASE_INDEX_FILES: int = 1500

# Maximum file size in bytes to read into the index.
CODEBASE_INDEX_MAX_FILE_BYTES: int = 128 * 1024

# Default number of results returned by search_codebase.
SEARCH_CODEBASE_TOP_K: int = 5

# ---------------------------------------------------------------------------
# Default model/thinking constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL: str = PROVIDERS["deepseek"].default_model
DEFAULT_THINKING: ThinkingMode = PROVIDERS["deepseek"].default_thinking
DEFAULT_PLANNER_MODEL: str = "deepseek-v4-flash"
DEFAULT_WORKER_MODEL: str = "deepseek-v4-pro"
DEFAULT_PLANNER_THINKING: ThinkingMode = "off"
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
    auto_commit_enabled: bool = True
    auto_dispatch: bool = False
    auto_approve: bool = False
    sandbox_mode: str = DEFAULT_SANDBOX_MODE
    tavily_api_key: str = ""
    first_launch_done: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        s = cls()
        # Flags
        if isinstance(data.get("first_launch_done"), bool):
            s.first_launch_done = data["first_launch_done"]
        if isinstance(data.get("tavily_api_key"), str):
            s.tavily_api_key = data["tavily_api_key"]
        # Provider
        if isinstance(data.get("provider"), str):
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
        if isinstance(data.get("auto_commit_enabled"), bool):
            s.auto_commit_enabled = data["auto_commit_enabled"]
        if isinstance(data.get("auto_dispatch"), bool):
            s.auto_dispatch = data["auto_dispatch"]
        if isinstance(data.get("auto_approve"), bool):
            s.auto_approve = data["auto_approve"]
        if isinstance(data.get("sandbox_mode"), str) and data["sandbox_mode"] in ("host", "docker", "wasm"):
            s.sandbox_mode = data["sandbox_mode"]
        return s


# ---------------------------------------------------------------------------
# Paths and file helpers
# ---------------------------------------------------------------------------


def get_subprocess_kwargs() -> dict[str, Any]:
    """Return kwargs for subprocess.run/Popen to suppress console flashes on Windows."""
    import subprocess
    import sys
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # CREATE_NO_WINDOW prevents the console window from appearing.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        # STARTUPINFO SW_HIDE is a belt-and-suspenders approach for some environments.
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    return kwargs


from aura.resources import get_resource_path


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
    return get_resource_path("media/AurA.ico")


def media_path(name: str) -> Path:
    """Return the absolute path to a file in the project media/ directory."""
    return get_resource_path(f"media/{name}")


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
