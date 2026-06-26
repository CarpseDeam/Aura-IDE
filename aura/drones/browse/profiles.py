"""Persistent browser profile path management for Browse Drone."""

from __future__ import annotations

import re
from pathlib import Path

from aura.paths import data_dir


def safe_profile_name(name: str) -> str:
    """Sanitize a profile name to a safe filesystem-friendly string.

    Keeps alphanumeric, dash, underscore, and dot characters.
    Replaces all other characters with ``_``.
    Falls back to ``"default"`` if the result is empty.
    """
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)
    # Collapse consecutive underscores
    safe = re.sub(r"_+", "_", safe)
    if not safe:
        return "default"
    return safe


def profile_dir(name: str) -> Path:
    """Return the path for a named profile directory without creating it."""
    return data_dir() / "browse_profiles" / safe_profile_name(name)


def ensure_profile_dir(name: str) -> Path:
    """Return the path for a named profile directory, creating it if needed.

    Creates the full directory tree.  Playwright's
    ``launch_persistent_context`` will use this directory for profile data.
    """
    path = profile_dir(name)
    path.mkdir(parents=True, exist_ok=True)
    return path
