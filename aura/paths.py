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


def safe_relative_to(path: Path | str, root: Path | str) -> Path:
    """Safely compute relative path, handling Windows case-insensitivity."""
    import os
    p_path: Path = Path(path)
    p_root: Path = Path(root)
    try:
        return p_path.resolve().relative_to(p_root.resolve())
    except ValueError:
        try:
            return Path(os.path.relpath(p_path, p_root))
        except Exception:
            return p_path


def safe_is_relative_to(path: Path | str, root: Path | str) -> bool:
    """Safely check if a path is relative to (under) a root directory."""
    import os
    p_path: Path = Path(path)
    p_root: Path = Path(root)
    try:
        p_resolved: Path = p_path.resolve()
        r_resolved: Path = p_root.resolve()
        rel: str = os.path.relpath(p_resolved, r_resolved)
        return not (rel.startswith("..") or os.path.isabs(rel))
    except Exception:
        try:
            return p_path.is_relative_to(p_root)
        except Exception:
            return False

