"""Settings, paths, model registry, and pricing constants for Aura."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aura.paths import APP_NAME, APP_AUTHOR, config_dir, data_dir
from aura.key_manager import get_key as _stored_get_key, has_key as _stored_has_key, set_key as _stored_set_key
from aura.providers.registry import provider_registry
from aura.providers.base import ModelInfo, ProviderSpec as ProviderConfig, ProviderId, ThinkingMode, ModelId
from aura.providers.catalog import (
    DEFAULT_MODEL,
    DEFAULT_THINKING,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_WORKER_MODEL,
    DEFAULT_PLANNER_THINKING,
    DEFAULT_WORKER_THINKING,
)
from aura.models import get_pricing, cost_usd, PROVIDERS
from aura.settings import (
    AppSettings,
    load_settings,
    save_settings,
    resolve_role_default_model,
    DEFAULT_PROVIDER,
    DEFAULT_SANDBOX_MODE,
    DEFAULT_VISION_ENABLED,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_ENDPOINT,
)

# Backward-compatible aliases

DEEPSEEK_BASE_URL: str = provider_registry.get("deepseek").base_url
ENV_API_KEY: str = provider_registry.get("deepseek").env_key

# Deprecated module-level model dict — kept for backward compat with smoke scripts.
MODELS: dict[str, ModelInfo] = dict(provider_registry.get("deepseek").models)

# Tavily (web search) API key

TAVILY_API_KEY_ENV: str = "TAVILY_API_KEY"

# Convenience accessors


def get_provider(provider_id: str) -> ProviderConfig:
    return provider_registry.get(provider_id)


def get_api_key(provider_id: str) -> str | None:
    """Check env var first, then hardware-encrypted stored key."""
    cfg = provider_registry.get(provider_id)

    # 1. Environment variable takes precedence.
    if cfg.env_key:
        val: str | None = os.environ.get(cfg.env_key)
        if val:
            return val

    # 1b. Extra fallback for google_cloud using GOOGLE_API_KEY
    if provider_id == "google_cloud":
        google_val: str | None = os.environ.get("GOOGLE_API_KEY")
        if google_val:
            return google_val

    # 2. Stored key (hardware-tethered, auto-migrates legacy plaintext)
    return _stored_get_key(provider_id)


def resolve_api_key(provider_id: str) -> str:
    """Like require_api_key but provider-aware. Raises RuntimeError if not found."""
    key = get_api_key(provider_id)
    if not key:
        cfg = provider_registry.get(provider_id)
        raise RuntimeError(
            f"No API key found for {cfg.label}. "
            f"Set the {cfg.env_key} environment variable or "
            f"open Settings → API Keys to add one."
        )
    return key


def has_api_key(provider_id: str | None = None) -> bool:
    """If provider_id is None, checks the default provider (deepseek)."""
    pid = provider_id if provider_id is not None else DEFAULT_PROVIDER
    return get_api_key(pid) is not None


def has_usable_provider_credentials() -> bool:
    """Return True if at least one provider has a usable API key (env var or stored)."""
    for pid in provider_registry.ids():
        if get_api_key(pid) is not None:
            return True
    return False


def require_api_key() -> str:
    """Legacy wrapper — checks the default (DeepSeek) provider."""
    return resolve_api_key(DEFAULT_PROVIDER)


def set_api_key(provider_id: str, api_key: str) -> None:
    """Store an API key encrypted with a hardware-derived key."""
    _stored_set_key(provider_id, api_key)


def list_stored_providers() -> list[str]:
    """Return list of provider IDs that have a stored key (encrypted or legacy)."""
    result: list[str] = []
    for pid in provider_registry.ids():
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


def fetch_provider_models(provider_id: str) -> tuple[dict[str, ModelInfo], dict[str, dict[str, float]], str | None]:
    """Fetch models and pricing from the provider's API.

    Returns (models_dict, pricing_dict, error_message).
    """
    try:
        client = provider_registry.create_client(provider_id)
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

            name = m.get("name") or mid
            p = m.get("pricing", {})
            try:
                in_m = float(p.get("prompt", 0)) * 1_000_000
                out_m = float(p.get("completion", 0)) * 1_000_000
                hit_m = float(p.get("request", 0)) * 1_000_000 or (in_m * 0.5)
            except (ValueError, TypeError):
                in_m = out_m = hit_m = 0.0

            modalities = m.get("architecture", {}).get("modalities", [])
            supports_vision = "image" in modalities

            models[mid] = ModelInfo(
                id=mid,
                label=name,
                input_per_m_usd=in_m,
                output_per_m_usd=out_m,
                cache_hit_per_m_usd=hit_m,
                supports_vision=supports_vision,
            )
            pricing[mid] = {"in_miss": in_m, "in_hit": hit_m, "out": out_m}
    else:
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
            for p_cfg in provider_registry.all().values():
                if mid in p_cfg.models:
                    supports_vision = p_cfg.models[mid].supports_vision
                    break

            models[mid] = ModelInfo(
                id=mid,
                label=label,
                input_per_m_usd=in_m,
                output_per_m_usd=out_m,
                cache_hit_per_m_usd=hit_m,
                supports_vision=supports_vision,
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


def redact_secrets(text: str) -> str:
    """Redact all known API keys from the given text."""
    if not text:
        return text

    keys_to_redact = set()
    for pid in provider_registry.ids():
        try:
            key = get_api_key(pid)
            if key:
                keys_to_redact.add(key)
        except Exception:
            pass

    tavily_key = get_tavily_api_key()
    if tavily_key:
        keys_to_redact.add(tavily_key)

    redacted = text
    for key in keys_to_redact:
        if len(key) > 5:
            redacted = redacted.replace(key, "[REDACTED]")

    return redacted


# Pricing helpers

PRICING: dict[str, dict[str, float]] = dict(provider_registry.get("deepseek").pricing)


# Hard limits

MAX_TOOL_ROUNDS = 50
MAX_READ_BYTES = 200 * 1024
MAX_GLOB_RESULTS = 200
MAX_CONTEXT_TOKENS = 60_000
TRUNCATE_TOOL_RESULT_CHARS = 500

SKIP_DIRS = {
    "__pycache__",
    ".venv",
    ".git",
    "node_modules",
    ".import",
    ".aura",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
SKIP_FILE_SUFFIXES = {".import"}

# Codebase index (BM25 search_codebase tool)

CODEBASE_INDEX_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".gd", ".cpp", ".c", ".h", ".hpp",
    ".rs", ".go", ".java", ".rb", ".php", ".swift", ".kt", ".scala",
    ".cfg", ".toml", ".yaml", ".yml", ".json", ".xml", ".ini",
    ".md", ".rst", ".txt", ".sh", ".bash", ".zsh", ".fish",
    ".css", ".scss", ".less", ".html", ".vue", ".svelte",
    ".sql", ".r", ".lua", ".zig", ".odin",
}

MAX_CODEBASE_INDEX_FILES: int = 1500
CODEBASE_INDEX_MAX_FILE_BYTES: int = 128 * 1024
SEARCH_CODEBASE_TOP_K: int = 5


# Paths and file helpers


def get_subprocess_kwargs() -> dict[str, Any]:
    """Return kwargs for subprocess.run/Popen to suppress console flashes on Windows."""
    import subprocess
    import sys
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
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


def save_dynamic_catalog(provider_id: str, models: dict[str, ModelInfo], pricing: dict[str, dict[str, float]]) -> None:
    """Save dynamically fetched models to a local cache file."""
    if not provider_registry.has(provider_id):
        return

    path = catalog_cache_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    models_raw = {k: asdict(v) for k, v in models.items()}
    data[provider_id] = {
        "models": models_raw,
        "pricing": pricing,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_dynamic_catalog() -> None:
    """Load cached models and update the provider registry."""
    path = catalog_cache_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    for pid, entry in data.items():
        if not provider_registry.has(pid):
            continue
        cfg = provider_registry.get(pid)

        cached_models = entry.get("models", {})
        cached_pricing = entry.get("pricing", {})

        for mid, m_data in cached_models.items():
            if mid in cfg.models:
                hc_m = cfg.models[mid]
                if m_data.get("input_per_m_usd", 0.0) == 0.0 and m_data.get("output_per_m_usd", 0.0) == 0.0:
                    if hc_m.input_per_m_usd > 0.0 or hc_m.output_per_m_usd > 0.0:
                        m_data["input_per_m_usd"] = hc_m.input_per_m_usd
                        m_data["output_per_m_usd"] = hc_m.output_per_m_usd
                        m_data["cache_hit_per_m_usd"] = hc_m.cache_hit_per_m_usd
            cfg.models[mid] = ModelInfo(**m_data)
            
        for mid, p_data in cached_pricing.items():
            if p_data.get("in_miss", 0.0) == 0.0 and p_data.get("out", 0.0) == 0.0:
                hc_p = cfg.pricing.get(mid, {})
                if hc_p.get("in_miss", 0.0) > 0.0 or hc_p.get("out", 0.0) > 0.0:
                    continue
            cfg.pricing[mid] = p_data


# Restore dynamic models on load
load_dynamic_catalog()
