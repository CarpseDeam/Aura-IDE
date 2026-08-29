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


def resolve_production_default_model(provider_id: ProviderId | None) -> str:
    """Return the configured default model for Aura's production provider."""
    from aura.providers.registry import provider_registry

    if not provider_id or not provider_registry.has(provider_id):
        return DEFAULT_MODEL

    cfg = provider_registry.get(provider_id)
    return cfg.default_model


logger = logging.getLogger(__name__)

@dataclass
class AppSettings:
    provider: ProviderId = DEFAULT_PROVIDER
    default_model: str = DEFAULT_MODEL
    default_thinking: ThinkingMode = DEFAULT_THINKING
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
        # Provider
        s.provider = _provider_from_data(data, "provider", s.provider)
        # Models
        s.default_model = _model_from_data(data, "default_model", s.provider)
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
    if raw in ("google_ai", "vertex_ai"):  # removed providers
        return None
    if raw == "aura":  # removed Aura Credits provider
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


def _provider_from_data(
    data: dict[str, Any], key: str, current: ProviderId
) -> ProviderId:
    raw = data.get(key)
    if not isinstance(raw, str):
        return current
    # Auto-migrate removed Google providers to DeepSeek.
    if raw in ("google_ai", "vertex_ai", "aura"):
        logger.warning(
            "Migrating removed provider %s (%r) -> %s",
            key,
            raw,
            DEFAULT_PROVIDER,
        )
        return DEFAULT_PROVIDER
    if provider_registry.has(raw):
        return cast(ProviderId, raw)

    logger.warning(
        "Invalid provider value for %s: %r; falling back to %s",
        key,
        raw,
        DEFAULT_PROVIDER,
    )
    return DEFAULT_PROVIDER


def _model_from_data(data: dict[str, Any], key: str, provider: ProviderId) -> str:
    provider_cfg = provider_registry.get(provider)
    raw = data.get(key)
    if isinstance(raw, str) and raw in provider_cfg.models:
        return raw

    if isinstance(raw, str):
        logger.warning(
            "Invalid model value for %s: %r is not available for provider %s; "
            "falling back to %s",
            key,
            raw,
            provider,
            provider_cfg.default_model,
        )
    elif key in data:
        logger.warning(
            "Invalid model value for %s: %r; falling back to %s",
            key,
            raw,
            provider_cfg.default_model,
        )

    return provider_cfg.default_model


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
    data = asdict(settings)
    data["companion_enabled"] = False  # session-only; never persist as enabled
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
