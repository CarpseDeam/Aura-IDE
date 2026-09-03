from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from aura.companion.defaults import (
    DEFAULT_HOSTED_COMPANION_RELAY_URL,
    DEFAULT_HOSTED_COMPANION_WEB_URL,
    DEFAULT_LOCAL_COMPANION_RELAY_URL,
    DEFAULT_LOCAL_COMPANION_WEB_URL,
)
from aura.models import (
    DEFAULT_MODEL,
    DEFAULT_THINKING,
    ProviderId,
    ThinkingMode,
)
from aura.paths import config_dir
from aura.providers.base import normalize_thinking_mode
from aura.providers.local_openai import (
    DEFAULT_LOCAL_OPENAI_BASE_URL,
    normalize_local_openai_base_url,
)
from aura.providers.registry import provider_registry

# Default-ish old localhost variants that should migrate to hosted
_RELAY_DEFAULT_VARIANTS = frozenset({
    "",
    "ws://localhost:8765",
    "ws://localhost:8765/ws",
    "ws://127.0.0.1:8765",
    "ws://127.0.0.1:8765/ws",
    "ws://[::1]:8765",
    "localhost:8765",
})
_WEB_DEFAULT_VARIANTS = frozenset({
    "",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://[::1]:5173",
    "localhost:5173",
})

DEFAULT_PROVIDER: ProviderId = "deepseek"
DEFAULT_SANDBOX_MODE: str = "host"

#: Providers that were once selectable and are no longer registered. A saved
#: selection naming one of these migrates to ``DEFAULT_PROVIDER``.
REMOVED_PROVIDERS: frozenset[str] = frozenset({
    "google_ai",
    "vertex_ai",
    "aura",
    "claude_code",
    "codex",
})

#: Model ids that only ever existed on a removed provider. They are refused on
#: their own so a migrated provider can never be paired with a stale model id.
REMOVED_PROVIDER_MODELS: frozenset[str] = frozenset({
    "claude-code",
    "codex",
})


def resolve_production_default_model(provider_id: ProviderId | None) -> str:
    """Return the configured default model for Aura's production provider."""
    from aura.providers.registry import provider_registry

    if not provider_id or not provider_registry.has(provider_id):
        return DEFAULT_MODEL

    cfg = provider_registry.get(provider_id)
    if cfg.default_model:
        return cfg.default_model
    # Discovery-only providers deliberately carry no invented default. Once a
    # real model snapshot exists, its first entry is the usable fallback.
    return next(iter(cfg.models), "")


logger = logging.getLogger(__name__)

@dataclass
class AppSettings:
    provider: ProviderId = DEFAULT_PROVIDER
    default_model: str = DEFAULT_MODEL
    default_thinking: ThinkingMode = DEFAULT_THINKING
    local_openai_base_url: str = DEFAULT_LOCAL_OPENAI_BASE_URL
    restore_last_conversation: bool = True
    temperature: float = 0.7
    auto_approve: bool = False
    #: When True, Aura pauses before the first workspace mutation of a real
    #: user turn so the user can review/edit the implementation plan via the
    #: ``review_implementation_plan`` tool. Off by default so existing
    #: production behavior is unchanged until the user opts in.
    review_plan_before_changes: bool = False
    sandbox_mode: str = DEFAULT_SANDBOX_MODE
    terminal_window_geometry: str = ""
    main_window_geometry: str = ""
    main_window_state: str = ""
    main_splitter_sizes: list[int] = field(default_factory=list)
    playground_outer_splitter_sizes: list[int] = field(default_factory=list)
    playground_vertical_splitter_sizes: list[int] = field(default_factory=list)
    first_launch_done: bool = False
    onboarding_checklist: dict = field(default_factory=dict)
    onboarding_version: int = 1
    # Companion (mobile control plane)
    companion_enabled: bool = False
    companion_relay_url: str = DEFAULT_HOSTED_COMPANION_RELAY_URL
    companion_display_name: str = ""
    companion_web_url: str = DEFAULT_HOSTED_COMPANION_WEB_URL
    # Windows Computer Use (structured Windows UI Automation over MCP).
    # Off by default and persisted honestly, unlike companion_enabled: this
    # grants no remote control, and a user who turned it on last session
    # expects their tools back without re-enabling them every launch.
    windows_computer_use_enabled: bool = False
    #: A command to launch instead of the managed install. Non-empty means
    #: "use exactly this", so managed installation is bypassed entirely.
    windows_computer_use_command: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        s = cls()
        raw_local_endpoint = data.get("local_openai_base_url")
        if isinstance(raw_local_endpoint, str):
            s.local_openai_base_url = normalize_local_openai_base_url(
                raw_local_endpoint
            )
        provider_registry.configure_local_base_url(s.local_openai_base_url)
        # Flags
        if isinstance(data.get("first_launch_done"), bool):
            s.first_launch_done = data["first_launch_done"]
        if isinstance(data.get("terminal_window_geometry"), str):
            s.terminal_window_geometry = data["terminal_window_geometry"]
        if isinstance(data.get("main_window_geometry"), str):
            s.main_window_geometry = data["main_window_geometry"]
        if isinstance(data.get("main_window_state"), str):
            s.main_window_state = data["main_window_state"]
        if isinstance(data.get("main_splitter_sizes"), list):
            s.main_splitter_sizes = data["main_splitter_sizes"]
        # Playground splitter sizes — validate expected length 2, sum > 0, each >= 40
        raw_outer = data.get("playground_outer_splitter_sizes")
        if isinstance(raw_outer, list) and len(raw_outer) == 2 and sum(raw_outer) > 0 and all(isinstance(v, int) and v >= 40 for v in raw_outer):
            s.playground_outer_splitter_sizes = raw_outer
        raw_vertical = data.get("playground_vertical_splitter_sizes")
        if isinstance(raw_vertical, list) and len(raw_vertical) == 2 and sum(raw_vertical) > 0 and all(isinstance(v, int) and v >= 40 for v in raw_vertical):
            s.playground_vertical_splitter_sizes = raw_vertical
        # Provider and model resolve as one unit, so a removed provider never
        # leaves its retired model id attached to the replacement provider.
        s.provider, s.default_model = migrate_provider_and_model(
            data.get("provider"),
            data.get("default_model"),
            fallback_provider=s.provider,
        )
        thinking = normalize_thinking_mode(data.get("default_thinking"))
        if thinking is not None:
            s.default_thinking = thinking
        if isinstance(data.get("restore_last_conversation"), bool):
            s.restore_last_conversation = data["restore_last_conversation"]
        # Temperature
        if "temperature" in data:
            raw = data["temperature"]
            if isinstance(raw, (int, float)):
                s.temperature = max(0.0, min(2.0, float(raw)))
        if isinstance(data.get("auto_approve"), bool):
            s.auto_approve = data["auto_approve"]
        if isinstance(data.get("review_plan_before_changes"), bool):
            s.review_plan_before_changes = data["review_plan_before_changes"]
        if isinstance(data.get("sandbox_mode"), str) and data["sandbox_mode"] in ("host", "docker", "wasm"):
            s.sandbox_mode = data["sandbox_mode"]
        # Companion — relay URL, web URL, and display name are persistent config.
        # companion_enabled is session-only: Aura must never auto-start remote
        # control on launch, so it is always forced False regardless of what was saved.
        s.companion_enabled = False

        # Companion URL migration: old localhost defaults → hosted defaults
        migrated_relay = data.get("companion_relay_url", "")
        migrated_web = data.get("companion_web_url", "")
        if migrated_relay in _RELAY_DEFAULT_VARIANTS:
            migrated_relay = DEFAULT_HOSTED_COMPANION_RELAY_URL
        if migrated_web in _WEB_DEFAULT_VARIANTS:
            migrated_web = DEFAULT_HOSTED_COMPANION_WEB_URL
        s.companion_relay_url = migrated_relay
        s.companion_web_url = migrated_web

        # Preserve explicit custom values (not matching any default variant)
        if isinstance(data.get("companion_relay_url"), str) and data["companion_relay_url"] not in _RELAY_DEFAULT_VARIANTS:
            s.companion_relay_url = data["companion_relay_url"]
        if isinstance(data.get("companion_display_name"), str):
            s.companion_display_name = data["companion_display_name"]
        if isinstance(data.get("companion_web_url"), str) and data["companion_web_url"] not in _WEB_DEFAULT_VARIANTS:
            s.companion_web_url = data["companion_web_url"]

        # Dev override: AURA_COMPANION_DEV_LOCAL=1 → localhost defaults
        if os.environ.get("AURA_COMPANION_DEV_LOCAL") == "1":
            s.companion_relay_url = DEFAULT_LOCAL_COMPANION_RELAY_URL
            s.companion_web_url = DEFAULT_LOCAL_COMPANION_WEB_URL
        # Windows Computer Use
        if isinstance(data.get("windows_computer_use_enabled"), bool):
            s.windows_computer_use_enabled = data["windows_computer_use_enabled"]
        if isinstance(data.get("windows_computer_use_command"), str):
            s.windows_computer_use_command = data["windows_computer_use_command"].strip()
        # Onboarding fields (backward-compatible)
        if isinstance(data.get("onboarding_checklist"), dict):
            s.onboarding_checklist = data["onboarding_checklist"]
        if isinstance(data.get("onboarding_version"), int):
            s.onboarding_version = data["onboarding_version"]
        return s


def _valid_provider(raw: Any) -> ProviderId | None:
    """Return *raw* as a ProviderId when it names a currently registered provider."""
    if not isinstance(raw, str) or not raw:
        return None
    if raw in REMOVED_PROVIDERS:
        return None
    if provider_registry.has(raw):
        return cast(ProviderId, raw)
    return None


def _valid_model(raw: Any, provider: ProviderId) -> str | None:
    """Return *raw* when it is a model available for *provider*."""
    if not isinstance(raw, str) or not raw:
        return None
    if not provider_registry.has(provider):
        return None
    return raw if raw in provider_registry.get(provider).models else None


def _migrated_provider(raw: str) -> tuple[ProviderId, bool]:
    """Resolve a persisted provider id. Returns ``(provider, migrated)``."""
    if raw in REMOVED_PROVIDERS:
        logger.warning("Migrating removed provider %r -> %s", raw, DEFAULT_PROVIDER)
        return DEFAULT_PROVIDER, True
    if provider_registry.has(raw):
        return cast(ProviderId, raw), False

    logger.warning(
        "Invalid provider value %r; falling back to %s", raw, DEFAULT_PROVIDER
    )
    return DEFAULT_PROVIDER, True


def _migrated_model(raw: Any, provider: ProviderId, *, strict: bool) -> str:
    default_model = resolve_production_default_model(provider)

    if isinstance(raw, str) and raw in REMOVED_PROVIDER_MODELS:
        logger.warning(
            "Migrating removed provider model %r -> %s", raw, default_model
        )
        return default_model

    if isinstance(raw, str) and raw:
        provider_spec = provider_registry.get(provider)
        if (
            not strict
            or raw in provider_spec.models
            # Local catalogs are loaded by ``aura.config``. Preserve the
            # persisted selection when this lightweight settings module is
            # used on its own before that cache has been hydrated; the local
            # provider still remains unusable until discovery supplies models.
            or (provider_spec.kind == "local" and not provider_spec.models)
        ):
            return raw
        logger.warning(
            "Invalid model value: %r is not available for provider %s; "
            "falling back to %s",
            raw,
            provider,
            default_model,
        )
    elif raw is not None:
        logger.warning(
            "Invalid model value: %r; falling back to %s", raw, default_model
        )

    return default_model


def migrate_provider_and_model(
    provider_raw: Any,
    model_raw: Any,
    *,
    fallback_provider: ProviderId = DEFAULT_PROVIDER,
    strict_model: bool = True,
) -> tuple[ProviderId, str]:
    """Resolve a persisted provider/model pair, migrating stale values together.

    Provider and model always move as one unit.  When the saved provider is no
    longer registered — the removed ``claude_code`` and ``codex`` CLI entries
    included — the saved model belonged to that retired provider, so it is
    discarded along with it and the replacement provider's own default model is
    used.  A retired model id is refused on its own as well, so a pairing such
    as DeepSeek + ``claude-code`` can never be produced.

    ``strict_model`` controls the surviving-provider case.  Settings validate
    the model against the provider catalog (``True``).  Persisted conversations
    keep whatever model id they actually ran with (``False``), because the
    dynamic catalog is not authoritative for history.
    """
    if not isinstance(provider_raw, str) or not provider_raw:
        # No saved provider: keep the caller's current one and judge the model
        # against it.
        return fallback_provider, _migrated_model(
            model_raw, fallback_provider, strict=strict_model
        )

    provider, migrated = _migrated_provider(provider_raw)
    if migrated:
        return provider, resolve_production_default_model(provider)
    return provider, _migrated_model(model_raw, provider, strict=strict_model)


def settings_path() -> Path:
    return config_dir() / "config.json"


def load_settings() -> AppSettings:
    p = settings_path()
    if not p.exists():
        return AppSettings.from_dict({})  # Will migrate → hosted; honor AURA_COMPANION_DEV_LOCAL
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings.from_dict({})
    if not isinstance(data, dict):
        return AppSettings.from_dict({})
    return AppSettings.from_dict(data)


def save_settings(settings: AppSettings) -> None:
    p = settings_path()
    settings.local_openai_base_url = normalize_local_openai_base_url(
        settings.local_openai_base_url
    )
    provider_registry.configure_local_base_url(settings.local_openai_base_url)
    data = asdict(settings)
    data["companion_enabled"] = False  # session-only; never persist as enabled
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
