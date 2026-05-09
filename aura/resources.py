"""Resource resolution for dev and packaged (PyInstaller) environments."""
import sys
from pathlib import Path


def get_resource_path(relative_path: str | Path) -> Path:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = Path(sys._MEIPASS)
    except Exception:
        # Dev environment: base path is the project root (where media/ lives)
        # or the package root (where aura/ lives).
        # We'll assume resources are relative to this file's parent's parent (project root).
        base_path = Path(__file__).resolve().parent.parent

    return (base_path / relative_path).resolve()
