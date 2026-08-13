"""Settings, paths, model registry, and pricing constants for Aura."""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from aura.key_manager import get_key as _stored_get_key
from aura.key_manager import has_key as _stored_has_key
from aura.key_manager import set_key as _stored_set_key
from aura.models import PROVIDERS, cost_usd, get_pricing  # noqa: F401
from aura.paths import APP_AUTHOR, APP_NAME, config_dir, data_dir  # noqa: F401
from aura.providers.base import ModelId, ModelInfo, ProviderId, ThinkingMode  # noqa: F401
from aura.providers.base import ProviderSpec as ProviderConfig
from aura.providers.catalog import (  # noqa: F401
    DEFAULT_MODEL,
    DEFAULT_THINKING,
)
from aura.providers.registry import provider_registry
from aura.settings import (  # noqa: F401
    DEFAULT_PROVIDER,
    AppSettings,
    load_settings,
    resolve_production_default_model,
    save_settings,
)

# Backward-compatible aliases

DEEPSEEK_BASE_URL: str = provider_registry.get("deepseek").base_url
ENV_API_KEY: str = provider_registry.get("deepseek").env_key

# Deprecated module-level model dict — kept for backward compat with smoke scripts.
MODELS: dict[str, ModelInfo] = dict(provider_registry.get("deepseek").models)

# Convenience accessors


def get_provider(provider_id: str) -> ProviderConfig:
    return provider_registry.get(provider_id)


def get_provider_kind(provider_id: str) -> str:
    """Return the kind of a provider: 'api_key', 'external_cli', or 'local'."""
    return provider_registry.get(provider_id).kind


def provider_needs_api_key(provider_id: str) -> bool:
    """Return True if this provider requires an API key to function."""
    return get_provider_kind(provider_id) == "api_key"


def is_external_cli_available(provider_id: str) -> bool:
    """Check whether an external_cli provider's executable is on PATH."""
    if get_provider_kind(provider_id) != "external_cli":
        return False
    if provider_id == "claude_code":
        exe = "claude.exe" if os.name == "nt" else "claude"
    elif provider_id == "codex":
        exe = "codex.exe" if os.name == "nt" else "codex"
    else:
        return False
    return shutil.which(exe) is not None


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
    """Like require_api_key but provider-aware. Raises RuntimeError if not found.

    For external_cli and local providers, returns an empty string without error.
    """
    if not provider_needs_api_key(provider_id):
        logging.getLogger(__name__).warning(
            "resolve_api_key called for non-api_key provider '%s' — returning empty string.",
            provider_id,
        )
        return ""
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
    """If provider_id is None, checks the default provider (deepseek).

    Returns whether the provider has a stored or environment API key.
    For external_cli and local providers, always returns False — they don't use API keys.
    """
    pid = provider_id if provider_id is not None else DEFAULT_PROVIDER
    if not provider_needs_api_key(pid):
        return False
    return get_api_key(pid) is not None


def has_usable_provider_configuration(provider_id: str | None = None) -> bool:
    """Return True if a given provider (or any provider) is configured and usable.

    If provider_id is given:
      - api_key providers: usable if an API key is available
      - external_cli providers: usable if the CLI executable is on PATH
      - local providers: not yet supported, returns False

    If provider_id is None: returns True if at least one registered provider is usable.
    """
    if provider_id is not None:
        kind = get_provider_kind(provider_id)
        if kind == "api_key":
            return get_api_key(provider_id) is not None
        if kind == "external_cli":
            return is_external_cli_available(provider_id)
        # local providers not yet supported
        return False

    for pid in provider_registry.ids():
        if has_usable_provider_configuration(pid):
            return True
    return False


def has_usable_provider_credentials() -> bool:
    """Deprecated: prefer has_usable_provider_configuration().

    Return True if at least one provider is configured and usable."""
    return has_usable_provider_configuration()


def require_api_key() -> str:
    """Legacy wrapper — checks the default (DeepSeek) provider."""
    return resolve_api_key(DEFAULT_PROVIDER)


def set_api_key(provider_id: str, api_key: str) -> None:
    """Store an API key encrypted with a hardware-derived key."""
    _stored_set_key(provider_id, api_key)


def list_stored_providers() -> list[str]:
    """Return list of api_key provider IDs that have a stored key (encrypted or legacy)."""
    result: list[str] = []
    for pid in provider_registry.ids():
        if provider_needs_api_key(pid) and _stored_has_key(pid):
            result.append(pid)
    return result


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

            # OpenRouter advertises real context metadata; keep it when sane so
            # dynamic models get a real budget instead of the fallback.
            ctx_tokens = _coerce_token_count(m.get("context_length"))
            top_provider = m.get("top_provider")
            max_out = 0
            if isinstance(top_provider, dict):
                max_out = _coerce_token_count(top_provider.get("max_completion_tokens"))

            # OpenRouter's own upstream creation timestamp, kept for
            # newest-first presentation ordering. None (never 0/guessed) when
            # absent or unusable, so ordering can tell "no metadata" apart from
            # "created at the epoch".
            created = _coerce_token_count(m.get("created")) or None

            models[mid] = ModelInfo(
                id=mid,
                label=name,
                input_per_m_usd=in_m,
                output_per_m_usd=out_m,
                cache_hit_per_m_usd=hit_m,
                supports_vision=supports_vision,
                context_window_tokens=ctx_tokens,
                max_output_tokens=max_out,
                created=created,
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

            # A provider "models" listing rarely carries context metadata, so
            # inherit whatever the seeded catalog already knows rather than
            # letting a refresh downgrade a known model to the fallback.
            supports_vision = False
            ctx_tokens = 0
            max_out = 0
            for p_cfg in provider_registry.all().values():
                if mid in p_cfg.models:
                    known = p_cfg.models[mid]
                    supports_vision = known.supports_vision
                    ctx_tokens = known.context_window_tokens
                    max_out = known.max_output_tokens
                    break

            models[mid] = ModelInfo(
                id=mid,
                label=label,
                input_per_m_usd=in_m,
                output_per_m_usd=out_m,
                cache_hit_per_m_usd=hit_m,
                supports_vision=supports_vision,
                context_window_tokens=ctx_tokens,
                max_output_tokens=max_out,
            )
            pricing[mid] = {"in_miss": in_m, "in_hit": hit_m, "out": out_m}

    return models, pricing, None


def _coerce_token_count(value: Any) -> int:
    """Return a positive int token count, or 0 when the value is unusable."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


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
    for env_name, env_value in os.environ.items():
        if env_name.endswith(("_API_KEY", "_TOKEN")) and env_value:
            keys_to_redact.add(env_value)

    redacted = text
    for key in keys_to_redact:
        if len(key) > 5:
            redacted = redacted.replace(key, "[REDACTED]")

    return redacted


# Pricing helpers

PRICING: dict[str, dict[str, float]] = dict(provider_registry.get("deepseek").pricing)


# Hard limits

MAX_TOOL_ROUNDS = 300
MAX_READ_BYTES = 200 * 1024
MAX_GLOB_RESULTS = 200
TRUNCATE_TOOL_RESULT_CHARS = 500

# There is no context-window budgeting here, and no working-set cap policy.
# The send path replays canonical history unchanged (see
# ``aura.conversation.history.History.for_api``), so nothing resolves a
# per-request token budget to shape it against. Real model capacity metadata
# — context window and max output tokens — stays in the provider catalog
# (``aura.providers.base.ModelInfo``), where it describes the model rather
# than governing what Aura sends.

# Canonical repository traversal skip policy. This is the single literal
# definition (aura.fs_utils and aura.codebase_index used to keep competing
# copies); aura.repository_inventory imports these directly and is the
# policy owner for repository-wide candidate discovery — see that module for
# sensitive-file policy and the canonical discovery/metadata contract built
# on top of these two sets. Kept here (rather than in repository_inventory)
# because get_subprocess_kwargs below and these sets are both needed by
# modules that must not import each other in a cycle.
SKIP_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "node_modules",
    ".import",
    ".aura",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".svn",
    ".hg",
    "eggs",
    ".eggs",
}
SKIP_FILE_SUFFIXES = {
    ".import",
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".o", ".a", ".exe", ".bin", ".class", ".wasm", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".mp4", ".mov", ".mp3", ".wav",
    ".pdf", ".db", ".sqlite",
}

# Codebase index (BM25 search_codebase tool)

MAX_CODEBASE_INDEX_FILES: int = 1500
CODEBASE_INDEX_MAX_FILE_BYTES: int = 128 * 1024
CODEBASE_INDEX_MAX_WALK_SECONDS: float = 10.0
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


_MODEL_INFO_FIELDS: frozenset[str] = frozenset(f.name for f in fields(ModelInfo))


def _model_info_from_cache(
    m_data: dict,
    known: ModelInfo | None,
    *,
    trust_cached_capacity: bool = False,
) -> ModelInfo:
    """Build a ModelInfo from a cached dict without losing context metadata.

    Caches written before context metadata existed carry no window/output
    fields, and a plain ``ModelInfo(**m_data)`` would silently downgrade a
    known model to "unknown" and push it onto the conservative fallback
    budget. Unrecognised keys are dropped so a cache from a newer build cannot
    raise TypeError here.

    ``trust_cached_capacity`` says whether the cached window/output numbers are
    real provider-advertised metadata. Only OpenRouter advertises them; for
    every other provider a model listing carries no capacity at all, so the
    cached values are just a snapshot of whatever the seeded catalog said when
    the refresh ran. Letting that snapshot win means correcting a model's
    capacity in the catalog would silently do nothing for anyone who has ever
    refreshed models — so for known catalog entries the catalog is
    authoritative. Dynamically discovered models (``known is None``) keep
    whatever the cache holds either way.
    """
    clean = {k: v for k, v in m_data.items() if k in _MODEL_INFO_FIELDS}
    if known is not None:
        if not trust_cached_capacity:
            clean["context_window_tokens"] = known.context_window_tokens
            clean["max_output_tokens"] = known.max_output_tokens
        if _coerce_token_count(clean.get("context_window_tokens")) == 0:
            clean["context_window_tokens"] = known.context_window_tokens
        if _coerce_token_count(clean.get("max_output_tokens")) == 0:
            clean["max_output_tokens"] = known.max_output_tokens
    return ModelInfo(**clean)


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

        if pid == "openrouter":
            # A saved OpenRouter cache entry is a full discovered snapshot (see
            # save_dynamic_catalog / ModelsPage discovery), not an incremental
            # patch — so loading it must *replace* the seeded catalog rather
            # than merge into it, or a model OpenRouter has removed upstream
            # would survive forever just because it was once hardcoded in
            # OPENROUTER_MODELS. An empty cache entry is left alone so startup
            # still has the seed catalog to fall back on.
            if cached_models:
                existing_before = dict(cfg.models)
                cfg.models.clear()
                for mid, m_data in cached_models.items():
                    cfg.models[mid] = _model_info_from_cache(
                        m_data, existing_before.get(mid), trust_cached_capacity=True
                    )
                # OpenRouter pricing is authoritative — never overwrite zero
                # pricing with seed defaults (free models are genuinely free).
                cfg.pricing.clear()
                for mid, p_data in cached_pricing.items():
                    cfg.pricing[mid] = p_data
        else:
            for mid, m_data in cached_models.items():
                if mid in cfg.models:
                    hc_m = cfg.models[mid]
                    if m_data.get("input_per_m_usd", 0.0) == 0.0 and m_data.get("output_per_m_usd", 0.0) == 0.0:
                        if hc_m.input_per_m_usd > 0.0 or hc_m.output_per_m_usd > 0.0:
                            m_data["input_per_m_usd"] = hc_m.input_per_m_usd
                            m_data["output_per_m_usd"] = hc_m.output_per_m_usd
                            m_data["cache_hit_per_m_usd"] = hc_m.cache_hit_per_m_usd
                cfg.models[mid] = _model_info_from_cache(m_data, cfg.models.get(mid))

            for mid, p_data in cached_pricing.items():
                if p_data.get("in_miss", 0.0) == 0.0 and p_data.get("out", 0.0) == 0.0:
                    hc_p = cfg.pricing.get(mid, {})
                    if hc_p.get("in_miss", 0.0) > 0.0 or hc_p.get("out", 0.0) > 0.0:
                        continue
                cfg.pricing[mid] = p_data


# Restore dynamic models on load
load_dynamic_catalog()
