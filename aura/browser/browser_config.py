"""User-configurable browser policy for Aura research."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BrowserConfig:
    """User-selected browser policy for Aura's web research browser.

    The default selects Google Chrome on Windows, macOS, and Linux.
    If Chrome is not installed, the controller reports the failure —
    there is no hidden fallback to Playwright Chromium or another browser.
    """

    browser_id: str = "chrome"
    """Short identifier: ``chrome``, ``edge``, ``brave``, ``chromium``."""

    label: str = "Google Chrome"
    """Human-readable label."""

    executable_path: str | None = None
    """Resolved executable path (None = auto-detect at launch time)."""

    profile_dir: str | None = None
    """Aura research profile directory name (relative to Aura's data dir).

    Set by the controller at launch time.  The user can override this
    to point at a specific directory via future UI.
    """

    @classmethod
    def default(cls) -> BrowserConfig:
        """Return the platform-appropriate default browser config."""
        return cls(
            browser_id="chrome",
            label="Google Chrome",
            executable_path=None,
            profile_dir="research",
        )

    @classmethod
    def detect_chrome(cls) -> str | None:
        """Return the path to a Chrome executable, or ``None``.

        Checks standard install locations for the current platform.
        Returns ``None`` when no Chrome is found — the controller
        reports the failure rather than falling back.
        """
        is_win = sys.platform == "win32"

        if is_win:
            bases = [
                os.environ.get("ProgramFiles", "C:\\Program Files"),
                os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
                os.environ.get("LocalAppData", ""),
            ]
            for base in bases:
                if not base:
                    continue
                candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if candidate.is_file():
                    return str(candidate)

            # PATH fallback
            which = _which("chrome")
            if which:
                return which
            return None

        if sys.platform == "darwin":
            candidate = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            return candidate if os.path.isfile(candidate) else None

        # Linux
        for name in ("google-chrome", "google-chrome-stable", "chrome"):
            candidate = _which(name)
            if candidate:
                return candidate
        return None


def _which(name: str) -> str | None:
    """Minimal ``shutil.which`` that returns ``None`` instead of raising."""
    try:
        import shutil
        return shutil.which(name)
    except Exception:
        return None
