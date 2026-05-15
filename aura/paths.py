"""Path and directory management for Aura."""
import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "Aura"
APP_AUTHOR = "Aura"


def config_dir() -> Path:
    """Return the platform-specific user configuration directory for Aura."""
    override = os.environ.get("AURA_CONFIG_DIR")
    p = Path(override).expanduser() if override else Path(user_config_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """Return the platform-specific user data directory for Aura."""
    override = os.environ.get("AURA_DATA_DIR")
    p = Path(override).expanduser() if override else Path(user_data_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p
