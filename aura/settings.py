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

    DeepSeek gets role-specific defaults (worker → deepseek-v4-pro,
    planner → deepseek-v4-flash). All other providers use their
    configured default_model from the registry.
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
    planner_worker_mode: bool = True
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
    aura_pending_session_id: str = ""
    aura_pending_claim_secret: str = ""
    terminal_window_geometry: str = ""
    drone_reports_window_geometry: str = ""
    drone_workbay_window_geometry: str = ""
    main_window_geometry: str = ""
    main_window_state: str = ""
    main_splitter_sizes: list[int] = field(default_factory=list)
    first_launch_done: bool = False
    onboarding_checklist: dict = field(default_factory=dict)
    onboarding_version: int = 1
    humanizer_enabled: bool = True
    humanizer_gate_enabled: bool = False
    humanizer_gate_min_severity: str = "high"
    humanizer_feature_log: bool = False
    humanizer_observe: bool = False

    # Companion (mobile control plane)
    companion_enabled: bool = False
    companion_relay_url: str = DEFAULT_HOSTED_COMPANION_RELAY_URL
    companion_display_name: str = ""
    companion_web_url: str = DEFAULT_HOSTED_COMPANION_WEB_URL

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        s = cls()
        # Ensure the rich interactive UI is always enabled, ignoring old saved configs.
        s.planner_worker_mode = True
        # Humanizer
        if isinstance(data.get("humanizer_enabled"), bool):
            s.humanizer_enabled = data["humanizer_enabled"]
        if isinstance(data.get("humanizer_gate_enabled"), bool):
            s.humanizer_gate_enabled = data["humanizer_gate_enabled"]
        if isinstance(data.get("humanizer_feature_log"), bool):
            s.humanizer_feature_log = data["humanizer_feature_log"]
        if isinstance(data.get("humanizer_observe"), bool):
            s.humanizer_observe = data["humanizer_observe"]
        if isinstance(data.get("humanizer_gate_min_severity"), str):
            val = data["humanizer_gate_min_severity"].lower()
            if val in ("critical", "high", "medium", "low"):
                s.humanizer_gate_min_severity = val
        # Rounds
        if "max_tool_rounds" in data:
            raw = data["max_tool_rounds"]
            if isinstance(raw, int):
                s.max_tool_rounds = max(1, raw)
        # Flags
        if isinstance(data.get("first_launch_done"), bool):
            s.first_launch_done = data["first_launch_done"]
        if isinstance(data.get("aura_pending_session_id"), str):
            s.aura_pending_session_id = data["aura_pending_session_id"]
        if isinstance(data.get("aura_pending_claim_secret"), str):
            s.aura_pending_claim_secret = data["aura_pending_claim_secret"]
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
        if isinstance(data.get("default_thinking"), str) and data["default_thinking"] in ("off", "high", "max"):
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
        if isinstance(data.get("default_planner_thinking"), str) and data["default_planner_thinking"] in ("off", "high", "max"):
            s.default_planner_thinking = data["default_planner_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("default_worker_thinking"), str) and data["default_worker_thinking"] in ("off", "high", "max"):
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
        # Onboarding fields (backward-compatible)
        if isinstance(data.get("onboarding_checklist"), dict):
            s.onboarding_checklist = data["onboarding_checklist"]
        if isinstance(data.get("onboarding_version"), int):
            s.onboarding_version = data["onboarding_version"]
        # Mirror planner → legacy compatibility fields so old code paths
        # reading provider/default_model/default_thinking get the planner values.
        s.provider = s.planner_provider
        s.default_model = s.default_planner_model
        s.default_thinking = s.default_planner_thinking
        return s


def _provider_from_data(
    data: dict[str, Any], key: str, current: ProviderId
) -> ProviderId:
    raw = data.get(key)
    if not isinstance(raw, str):
        return current
    # Auto-migrate removed Google providers to DeepSeek.
    if raw in ("google_ai", "vertex_ai"):
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
