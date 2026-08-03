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
    DEFAULT_PLANNER_MODEL,
    DEFAULT_PLANNER_THINKING,
    DEFAULT_THINKING,
    DEFAULT_WORKER_MODEL,
    DEFAULT_WORKER_THINKING,
    ProviderId,
    ThinkingMode,
)
from aura.paths import config_dir
from aura.providers.base import THINKING_MODES
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
def resolve_role_default_model(provider_id: ProviderId | None, role: str) -> str:
    """Return the default model for a given provider + role combo.

    ``production`` is the normal product role and uses the provider's
    configured default model. DeepSeek keeps role-specific defaults for the
    legacy worker/planner roles. All other providers use their configured
    default_model from the registry.
    """
    from aura.providers.registry import provider_registry

    if not provider_id or not provider_registry.has(provider_id):
        return DEFAULT_MODEL

    cfg = provider_registry.get(provider_id)
    if role == "worker" and provider_id == "deepseek":
        from aura.providers.catalog import DEFAULT_WORKER_MODEL

        return DEFAULT_WORKER_MODEL
    if role == "planner" and provider_id == "deepseek":
        from aura.providers.catalog import DEFAULT_PLANNER_MODEL

        return DEFAULT_PLANNER_MODEL
    return cfg.default_model


logger = logging.getLogger(__name__)

@dataclass
class AppSettings:
    provider: ProviderId = DEFAULT_PROVIDER
    planner_provider: ProviderId = DEFAULT_PROVIDER
    worker_provider: ProviderId = DEFAULT_PROVIDER
    planner_backend: str = "default_api"
    worker_backend: str = "default_api"
    default_model: str = DEFAULT_MODEL
    default_thinking: ThinkingMode = DEFAULT_THINKING
    restore_last_conversation: bool = True
    # Legacy Planner/Worker dispatch toggle. Retained for backward compatibility
    # with old persisted configs; normal startup always runs production
    # single-agent mode regardless of this value.
    planner_worker_mode: bool = False
    default_planner_model: str = DEFAULT_PLANNER_MODEL
    default_worker_model: str = DEFAULT_WORKER_MODEL
    default_planner_thinking: ThinkingMode = DEFAULT_PLANNER_THINKING
    default_worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
    temperature: float = 0.7
    worker_temperature: float = 0.1
    system_prompt: str = ""
    planner_system_prompt: str = ""
    worker_system_prompt: str = ""
    auto_dispatch: bool = False
    auto_approve: bool = False
    auto_summon_drones: bool = False
    sandbox_mode: str = DEFAULT_SANDBOX_MODE
    max_tool_rounds: int = 300
    terminal_window_geometry: str = ""
    drone_reports_window_geometry: str = ""
    drone_workbay_window_geometry: str = ""
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
        # Rounds
        if "max_tool_rounds" in data:
            raw = data["max_tool_rounds"]
            if isinstance(raw, int):
                s.max_tool_rounds = max(1, raw)
        # Flags
        if isinstance(data.get("first_launch_done"), bool):
            s.first_launch_done = data["first_launch_done"]
        if isinstance(data.get("terminal_window_geometry"), str):
            s.terminal_window_geometry = data["terminal_window_geometry"]
        if isinstance(data.get("drone_reports_window_geometry"), str):
            s.drone_reports_window_geometry = data["drone_reports_window_geometry"]
        if isinstance(data.get("drone_workbay_window_geometry"), str):
            s.drone_workbay_window_geometry = data["drone_workbay_window_geometry"]
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
        s.planner_provider = _provider_from_data(
            data, "planner_provider", s.planner_provider
        )
        s.worker_provider = _provider_from_data(
            data, "worker_provider", s.worker_provider
        )
        # Backends
        if isinstance(data.get("planner_backend"), str):
            s.planner_backend = data["planner_backend"]
        if isinstance(data.get("worker_backend"), str):
            s.worker_backend = data["worker_backend"]
        # Models
        s.default_model = _model_from_data(data, "default_model", s.provider)
        if isinstance(data.get("default_thinking"), str) and data["default_thinking"] in _THINKING_VALUES:
            s.default_thinking = data["default_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("restore_last_conversation"), bool):
            s.restore_last_conversation = data["restore_last_conversation"]
        if isinstance(data.get("planner_worker_mode"), bool):
            s.planner_worker_mode = data["planner_worker_mode"]
        s.default_planner_model = _model_from_data(
            data, "default_planner_model", s.planner_provider
        )
        s.default_worker_model = _model_from_data(
            data, "default_worker_model", s.worker_provider
        )
        if isinstance(data.get("default_planner_thinking"), str) and data["default_planner_thinking"] in _THINKING_VALUES:
            s.default_planner_thinking = data["default_planner_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("default_worker_thinking"), str) and data["default_worker_thinking"] in _THINKING_VALUES:
            s.default_worker_thinking = data["default_worker_thinking"]  # type: ignore[assignment]
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
        if isinstance(data.get("auto_dispatch"), bool):
            s.auto_dispatch = data["auto_dispatch"]
        if isinstance(data.get("auto_approve"), bool):
            s.auto_approve = data["auto_approve"]
        if isinstance(data.get("auto_summon_drones"), bool):
            s.auto_summon_drones = data["auto_summon_drones"]
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
        # Production settings migration — the generic fields are the source of
        # truth for normal coding. Legacy Planner/Worker fields are preserved
        # above and are never destroyed here.
        _migrate_production_settings(s, data)
        return s


#: Accepted reasoning selections. Adding "auto" here is purely additive: an
#: existing saved "off"/"high"/"max" still validates and is loaded verbatim.
_THINKING_VALUES = THINKING_MODES


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


def _valid_thinking(raw: Any) -> ThinkingMode | None:
    if isinstance(raw, str) and raw in _THINKING_VALUES:
        return cast(ThinkingMode, raw)
    return None


def _migrate_production_settings(s: "AppSettings", data: dict[str, Any]) -> None:
    """Resolve the one production configuration from a persisted config dict.

    Order of preference:

    1. Valid generic production values (``provider``, ``default_model``,
       ``default_thinking``, ``temperature``).
    2. For older configurations whose meaningful values only exist in the
       Planner fields, migrate those into the production settings.
    3. Safe provider/model defaults.

    Legacy Planner and Worker fields are left untouched so old configurations
    keep round-tripping.
    """
    # --- provider -------------------------------------------------------
    provider = _valid_provider(data.get("provider"))
    if provider is None:
        provider = _valid_provider(data.get("planner_provider"))
        if provider is not None:
            logger.info(
                "Migrating planner_provider -> production provider: %s", provider
            )
    if provider is None:
        provider = DEFAULT_PROVIDER
    s.provider = provider

    # --- model ----------------------------------------------------------
    model = _valid_model(data.get("default_model"), provider)
    if model is None:
        model = _valid_model(data.get("default_planner_model"), provider)
        if model is not None:
            logger.info(
                "Migrating default_planner_model -> production model: %s", model
            )
    if model is None:
        if "default_model" in data or "default_planner_model" in data:
            logger.warning(
                "No valid production model in saved settings for provider %s; "
                "falling back to the provider default",
                provider,
            )
        model = resolve_role_default_model(provider, "production")
    s.default_model = model

    # --- thinking -------------------------------------------------------
    thinking = _valid_thinking(data.get("default_thinking"))
    if thinking is None:
        thinking = _valid_thinking(data.get("default_planner_thinking"))
        if thinking is not None:
            logger.info(
                "Migrating default_planner_thinking -> production thinking: %s",
                thinking,
            )
    if thinking is not None:
        s.default_thinking = thinking

    # Temperature is already a generic field and was parsed above; nothing to
    # migrate. planner_worker_mode is loaded verbatim and never forced — normal
    # startup enters production single-agent mode regardless of its value.


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

    # Role-specific fallbacks for DeepSeek.
    if key == "default_worker_model" and provider == "deepseek":
        return DEFAULT_WORKER_MODEL
    if key == "default_planner_model" and provider == "deepseek":
        return DEFAULT_PLANNER_MODEL
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
