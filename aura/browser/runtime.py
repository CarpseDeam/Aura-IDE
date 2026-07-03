"""Shared Playwright browser runtime lifecycle."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from aura.resources import get_resource_path


@dataclass
class BrowserChoice:
    """Describes a detected or fallback browser option."""

    id: str
    label: str
    channel: str | None
    executable_path: str | None
    source: str  # "installed" or "playwright"


def _detect_installed_browsers() -> list[BrowserChoice]:
    """Detect installed browsers on the current platform.

    Returns an ordered list of BrowserChoice objects — Chrome, Edge,
    Brave (if found), always ending with the Playwright Chromium fallback.

    This function does NOT import or touch Playwright, making it mockable.
    """
    choices: list[BrowserChoice] = []

    is_win = sys.platform == "win32"
    is_mac = sys.platform == "darwin"
    is_linux = sys.platform == "linux"

    # --- Chrome (channel="chrome") ---
    chrome_found = False
    if is_mac:
        candidate = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(candidate):
            chrome_found = True
    elif is_linux:
        chrome_found = (
            shutil.which("google-chrome") is not None
            or shutil.which("google-chrome-stable") is not None
        )
    elif is_win:
        _pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        _pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        _la = os.environ.get("LocalAppData", "")
        for _base in (_pf, _pf86, _la):
            if _base and os.path.isfile(os.path.join(_base, "Google", "Chrome", "Application", "chrome.exe")):
                chrome_found = True
                break
        if not chrome_found:
            chrome_found = shutil.which("chrome") is not None

    if chrome_found:
        choices.append(BrowserChoice(
            id="chrome",
            label="Google Chrome",
            channel="chrome",
            executable_path=None,
            source="installed",
        ))

    # --- Edge (channel="msedge") ---
    edge_found = False
    if is_mac:
        candidate = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        if os.path.isfile(candidate):
            edge_found = True
    elif is_linux:
        edge_found = shutil.which("microsoft-edge") is not None
    elif is_win:
        _pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        _pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        _la = os.environ.get("LocalAppData", "")
        for _base in (_pf86, _pf, _la):
            if _base and os.path.isfile(os.path.join(_base, "Microsoft", "Edge", "Application", "msedge.exe")):
                edge_found = True
                break
        if not edge_found:
            edge_found = shutil.which("msedge") is not None

    if edge_found:
        choices.append(BrowserChoice(
            id="edge",
            label="Microsoft Edge",
            channel="msedge",
            executable_path=None,
            source="installed",
        ))

    # --- Brave (only if simple; no Playwright channel, uses executable_path) ---
    brave_path = None
    if is_mac:
        candidate = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        if os.path.isfile(candidate):
            brave_path = candidate
    elif is_linux:
        brave_path = shutil.which("brave-browser")
    elif is_win:
        _pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        _pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        _la = os.environ.get("LocalAppData", "")
        for _base in (_pf, _pf86, _la):
            if _base:
                _candidate = os.path.join(_base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")
                if os.path.isfile(_candidate):
                    brave_path = _candidate
                    break
        if not brave_path:
            brave_path = shutil.which("brave")

    if brave_path:
        choices.append(BrowserChoice(
            id="brave",
            label="Brave",
            channel=None,
            executable_path=brave_path,
            source="installed",
        ))

    # Always append the Playwright Chromium fallback
    choices.append(BrowserChoice(
        id="chromium",
        label="Playwright Chromium",
        channel=None,
        executable_path=None,
        source="playwright",
    ))

    return choices


class BrowserRuntime:
    """Manages the Playwright browser startup, lifecycle, and teardown.

    Create an instance, call ``start()``, then use ``context`` for
    browsing.  Call ``close()`` when done.
    """

    def __init__(self, headless: bool = True, user_data_dir: Path | None = None) -> None:
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._pw = None
        self._browser = None
        self._context = None
        self._unavailable_reason = ""
        self._browser_id = ""
        self._browser_label = ""
        self._browser_source = ""
        self._attempted_routes: list[str] = []

    @property
    def unavailable_reason(self) -> str:
        """The reason the runtime is unavailable, or empty string."""
        return self._unavailable_reason

    @property
    def context(self):  # -> playwright.sync_api.BrowserContext | None
        """The active BrowserContext, or None if not started."""
        return self._context

    @property
    def route_metadata(self) -> dict:
        """Metadata about the selected browser route."""
        return {
            "browser_id": self._browser_id,
            "browser_label": self._browser_label,
            "browser_source": self._browser_source,
            "browser_persistent": self._user_data_dir is not None,
            "browser_visible": not self._headless,
            "attempted_routes": list(self._attempted_routes),
        }

    def start(self) -> bool:
        """Launch Playwright browser and create a browsing context.

        Attempts each detected browser in priority order (Chrome, Edge,
        Brave, Playwright Chromium fallback) and uses the first one that
        succeeds.  Returns True on success.  On total failure sets
        ``_unavailable_reason`` (including attempted route chain) and
        returns False — never raises.
        """
        self._attempted_routes = []
        self._browser_id = ""
        self._browser_label = ""
        self._browser_source = ""

        # --- subprocess.Popen guard -----------------------------------
        # Playwright's ``sync_playwright().start()`` spawns a Node.js
        # driver via ``subprocess.Popen`` with no ``creationflags``,
        # which opens a blank console window on Windows.  Temporarily
        # patch Popen so that *every* caller in this stack gets
        # CREATE_NO_WINDOW + SW_HIDE.
        _orig_popen = None
        if sys.platform == "win32":
            import subprocess as _sp

            _orig_popen = _sp.Popen

            def _silent_popen(*popen_args, **popen_kwargs):  # noqa: ANN202
                flags = popen_kwargs.get("creationflags", 0)
                if isinstance(flags, int) and not (flags & _sp.CREATE_NEW_CONSOLE):
                    popen_kwargs["creationflags"] = flags | _sp.CREATE_NO_WINDOW
                if "startupinfo" not in popen_kwargs:
                    si = _sp.STARTUPINFO()
                    si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                    si.wShowWindow = _sp.SW_HIDE
                    popen_kwargs["startupinfo"] = si
                return _orig_popen(*popen_args, **popen_kwargs)

            _sp.Popen = _silent_popen
        # ----------------------------------------------------------------

        try:
            try:
                import playwright.sync_api  # noqa: F401
            except ImportError as exc:
                self._unavailable_reason = str(exc)
                return False

            # Frozen/packaged detection
            if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS") or "__compiled__" in globals():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(get_resource_path("ms-playwright"))

            choices = _detect_installed_browsers()
            last_error = ""

            for choice in choices:
                try:
                    self._pw = playwright.sync_api.sync_playwright().start()

                    channel = choice.channel
                    executable_path = choice.executable_path

                    if self._user_data_dir is not None:
                        # Persistent profile mode — use launch_persistent_context
                        self._user_data_dir.mkdir(parents=True, exist_ok=True)
                        kwargs = {
                            "user_data_dir": str(self._user_data_dir),
                            "headless": self._headless,
                        }
                        if channel is not None:
                            kwargs["channel"] = channel
                        elif executable_path is not None:
                            kwargs["executable_path"] = executable_path
                        self._context = self._pw.chromium.launch_persistent_context(**kwargs)
                        # self._browser stays None — lifecycle tied to persistent context
                    else:
                        # Anonymous mode
                        kwargs = {"headless": self._headless}
                        if channel is not None:
                            kwargs["channel"] = channel
                        elif executable_path is not None:
                            kwargs["executable_path"] = executable_path
                        self._browser = self._pw.chromium.launch(**kwargs)
                        self._context = self._browser.new_context()

                    # Success — record route metadata
                    self._browser_id = choice.id
                    self._browser_label = choice.label
                    self._browser_source = choice.source
                    return True

                except Exception as exc:
                    self._attempted_routes.append(choice.id)
                    last_error = str(exc)
                    # Tear down partial state before trying next route
                    if self._context is not None:
                        self._context.close()
                        self._context = None
                    if self._browser is not None:
                        self._browser.close()
                        self._browser = None
                    if self._pw is not None:
                        self._pw.stop()
                        self._pw = None

            # All routes failed
            self._unavailable_reason = (
                f"All browser routes failed: {', '.join(self._attempted_routes)}. "
                f"Last error: {last_error}"
            )
            return False

        except Exception as exc:
            self._unavailable_reason = str(exc)
            # Tear down partial state — we are already in the error path
            if self._context is not None:
                self._context.close()
                self._context = None
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None
            return False

        # --- restore original Popen ---------------------------------
        finally:
            if _orig_popen is not None:
                import subprocess as _sp2
                _sp2.Popen = _orig_popen
        # ------------------------------------------------------------

    def close(self) -> None:
        """Shut down the browser and clean up resources."""
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None
