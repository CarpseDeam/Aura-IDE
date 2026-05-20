"""CLI tool executable resolution helpers."""

from __future__ import annotations

import os
import shutil
import sys


def resolve_cli_executable(name: str) -> str | None:
    """Find a CLI executable, searching npm directories when not on PATH.

    On all platforms, tries ``shutil.which(name)`` first (respects the
    process PATH and PATHEXT on Windows).  If that fails on Windows, also
    searches common npm global install directories that may not be on
    PATH when Aura is launched from a Start Menu shortcut.

    Args:
        name: Bare executable name, e.g. ``"gemini"``, ``"claude"``,
            ``"codex"`` — without extension.

    Returns:
        Full path to the executable if found, otherwise ``None``.
    """
    # 1. Fast path: standard PATH search (handles PATHEXT on Windows).
    path = shutil.which(name)
    if path is not None:
        return path

    # 2. Extra npm / node directories that may be missing from PATH.
    extra_dirs: list[str] = []

    if sys.platform == "win32":
        extra_dirs.extend(
            [
                os.path.expandvars(r"%APPDATA%\npm"),
                os.path.expandvars(r"%LOCALAPPDATA%\npm"),
                os.path.expandvars(r"%ProgramFiles%\nodejs"),
                os.path.expandvars(r"%ProgramFiles(x86)%\nodejs"),
                os.path.expanduser("~/AppData/Roaming/npm"),
                os.path.expanduser("~/AppData/Local/npm"),
                os.path.expanduser("~/AppData/Local/agy/bin"),
            ]
        )

    # Collect unique, existing directories.
    seen: set[str] = set()
    for d in extra_dirs:
        d = os.path.normpath(d)
        if d not in seen and os.path.isdir(d):
            seen.add(d)

    # Try each directory with PATHEXT extensions (Windows) or plain name.
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PY;.PYW;.CPL")
    extensions = pathext.split(os.pathsep) if sys.platform == "win32" else [""]

    for d in seen:
        for ext in extensions:
            candidate = os.path.join(d, name + ext)
            if os.path.isfile(candidate):
                return candidate

    return None
