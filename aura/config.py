"""Settings, paths, model registry, and pricing constants for Aura."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "Aura"
APP_AUTHOR = "Aura"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ENV_API_KEY = "DEEPSEEK_API_KEY"

ModelId = Literal["deepseek-v4-flash", "deepseek-v4-pro"]
ThinkingMode = Literal["off", "high", "max"]


@dataclass(frozen=True)
class ModelInfo:
    id: ModelId
    label: str
    input_per_m_usd: float
    output_per_m_usd: float
    cache_hit_per_m_usd: float


# Pricing as documented in the dispatch (Phase 2 will surface a cost meter).
MODELS: dict[ModelId, ModelInfo] = {
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

DEFAULT_MODEL: ModelId = "deepseek-v4-flash"
DEFAULT_THINKING: ThinkingMode = "high"

# Vision settings (local Ollama model)
DEFAULT_VISION_ENABLED = True
DEFAULT_VISION_MODEL = "llama3.2-vision"
DEFAULT_VISION_ENDPOINT = "http://localhost:11434/v1"


# Per-million USD pricing used by the status-bar cost meter.
# Keys: in_hit (cached input), in_miss (uncached input), out (output).
PRICING: dict[ModelId, dict[str, float]] = {
    "deepseek-v4-flash": {"in_miss": 0.14, "in_hit": 0.003, "out": 0.28},
    "deepseek-v4-pro": {"in_miss": 1.74, "in_hit": 0.015, "out": 3.48},
}


def cost_usd(
    model: ModelId,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    output_tokens: int,
) -> float:
    p = PRICING.get(model)
    if p is None:
        return 0.0
    return (
        cache_hit_tokens * p["in_hit"]
        + cache_miss_tokens * p["in_miss"]
        + output_tokens * p["out"]
    ) / 1_000_000

# Hard limit on tool-call rounds within a single user turn.
MAX_TOOL_ROUNDS = 50
# Cap for read_file and similar to avoid blowing the context window.
MAX_READ_BYTES = 200 * 1024
# Cap on glob results.
MAX_GLOB_RESULTS = 200

SKIP_DIRS = {"__pycache__", ".venv", ".git", "node_modules", ".import", ".aura"}
SKIP_FILE_SUFFIXES = {".import"}


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


def require_api_key() -> str:
    key = os.environ.get(ENV_API_KEY)
    if not key:
        raise RuntimeError(
            f"{ENV_API_KEY} environment variable is not set. "
            "Set it in your shell, then relaunch Aura."
        )
    return key


def has_api_key() -> bool:
    return bool(os.environ.get(ENV_API_KEY))


# ---- App settings (persisted JSON) ----------------------------------------


DEFAULT_PLANNER_MODEL: ModelId = "deepseek-v4-flash"
DEFAULT_WORKER_MODEL: ModelId = "deepseek-v4-pro"
DEFAULT_PLANNER_THINKING: ThinkingMode = "high"
DEFAULT_WORKER_THINKING: ThinkingMode = "high"


@dataclass
class AppSettings:
    default_model: ModelId = DEFAULT_MODEL
    default_thinking: ThinkingMode = DEFAULT_THINKING
    restore_last_conversation: bool = True
    planner_worker_mode: bool = True
    default_planner_model: ModelId = DEFAULT_PLANNER_MODEL
    default_worker_model: ModelId = DEFAULT_WORKER_MODEL
    default_planner_thinking: ThinkingMode = DEFAULT_PLANNER_THINKING
    default_worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
    vision_enabled: bool = DEFAULT_VISION_ENABLED
    vision_model: str = DEFAULT_VISION_MODEL
    vision_endpoint: str = DEFAULT_VISION_ENDPOINT

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        s = cls()
        if data.get("default_model") in PRICING:
            s.default_model = data["default_model"]  # type: ignore[assignment]
        if data.get("default_thinking") in ("off", "high", "max"):
            s.default_thinking = data["default_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("restore_last_conversation"), bool):
            s.restore_last_conversation = data["restore_last_conversation"]
        if isinstance(data.get("planner_worker_mode"), bool):
            s.planner_worker_mode = data["planner_worker_mode"]
        if data.get("default_planner_model") in PRICING:
            s.default_planner_model = data["default_planner_model"]  # type: ignore[assignment]
        if data.get("default_worker_model") in PRICING:
            s.default_worker_model = data["default_worker_model"]  # type: ignore[assignment]
        if data.get("default_planner_thinking") in ("off", "high", "max"):
            s.default_planner_thinking = data["default_planner_thinking"]  # type: ignore[assignment]
        if data.get("default_worker_thinking") in ("off", "high", "max"):
            s.default_worker_thinking = data["default_worker_thinking"]  # type: ignore[assignment]
        if isinstance(data.get("vision_enabled"), bool):
            s.vision_enabled = data["vision_enabled"]
        if isinstance(data.get("vision_model"), str):
            s.vision_model = data["vision_model"]
        if isinstance(data.get("vision_endpoint"), str):
            s.vision_endpoint = data["vision_endpoint"]
        return s


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
