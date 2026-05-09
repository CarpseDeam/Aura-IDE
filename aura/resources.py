"""Resource resolution for dev and packaged (PyInstaller) environments."""
import sys
from pathlib import Path


def get_resource_path(relative_path: str | Path) -> Path:
    """Get absolute path to resource, works for dev, PyInstaller, and Nuitka."""
    # 1. Check for PyInstaller
    if hasattr(sys, "_MEIPASS"):
        base_path = Path(sys._MEIPASS)
    # 2. Check for Nuitka (Nuitka usually handles __file__ correctly, but this is safer for onefile)
    elif "__compiled__" in globals():
        base_path = Path(__file__).resolve().parent.parent
    else:
        # 3. Dev environment
        base_path = Path(__file__).resolve().parent.parent

    return (base_path / relative_path).resolve()
