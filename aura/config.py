"""Settings, paths, model registry, and pricing constants for Aura."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aura.paths import APP_NAME, APP_AUTHOR, config_dir, data_dir
from aura.key_manager import get_key as _stored_get_key, has_key as _stored_has_key, set_key as _stored_set_key
from aura.models import (
    PROVIDERS,
    ModelInfo,
    ProviderConfig,
    ProviderId,
    ThinkingMode,
    ModelId,
    get_pricing,
    cost_usd,
    DEFAULT_MODEL,
    DEFAULT_THINKING,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_WORKER_MODEL,
    DEFAULT_PLANNER_THINKING,
    DEFAULT_WORKER_THINKING,
)
from aura.settings import (
    AppSettings,
    load_settings,
    save_settings,
    DEFAULT_PROVIDER,
    DEFAULT_SANDBOX_MODE,
    DEFAULT_VISION_ENABLED,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_ENDPOINT,
)

# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

DEEPSEEK_BASE_URL: str = PROVIDERS["deepseek"].base_url
ENV_API_KEY: str = PROVIDERS["deepseek"].env_key

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


# ---------------------------------------------------------------------------
# Hard limits
# ---------------------------------------------------------------------------

# Hard limit on model/tool-call rounds within a single user turn.
# Tool execution itself is bounded by aura.conversation.tool_limits.ToolLimitState.
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
