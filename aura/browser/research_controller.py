"""Research Browser Controller — single owner of browser lifecycle for web research.

This module provides ``ResearchBrowserController``, the sole place in Aura
that decides which browser executable to use, what profile directory to load,
what port to bind, how to launch the process, how to wait for CDP readiness,
how to connect, and how to navigate a page.

All other code that needs a browser for research goes through this controller.
It does *not* import or depend on ``aura.research.ui_contract``,
``aura.research.request``, Playwright ``BrowserRuntime``, or drone manifest
``visible``/``headless`` fields.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aura.paths import data_dir

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CDP_POLL_INTERVAL = 0.1
_CDP_POLL_MAX = 5.0  # total seconds before CDP readiness times out
_NAVIGATION_TIMEOUT = 12000
_CDP_PROBE_TIMEOUT = 3.0

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ControllerError(Exception):
    """Base exception for controller failures."""


class BrowserDetectionError(ControllerError):
    """Raised when no suitable browser executable is found."""


class BrowserLaunchError(ControllerError):
    """Raised when the browser process fails to start."""


class CdpReadyTimeout(ControllerError):
    """Raised when CDP does not become ready within the timeout."""


class CdpConnectError(ControllerError):
    """Raised when connecting to CDP fails."""


class NavigationError(ControllerError):
    """Raised when page navigation fails."""


# ---------------------------------------------------------------------------
# Receipt types
# ---------------------------------------------------------------------------


@dataclass
class BrowserReceipt:
    """Structured receipt returned from every controller operation.

    Every field is populated so that a blank-window issue is obvious
    from a single receipt.
    """

    controller_version: str = "1.0"
    browser_executable: str = ""
    browser_profile_dir: str = ""
    browser_pid: int | None = None
    cdp_url: str = ""
    requested_url: str = ""
    first_navigated_url: str = ""
    final_active_url: str = ""
    page_title: str = ""
    navigation_status: str = "not_started"
    phase_errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the browser was launched, connected, and navigated."""
        return (
            bool(self.browser_executable)
            and bool(self.cdp_url)
            and self.navigation_status == "success"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "controller_version": self.controller_version,
            "browser_executable": self.browser_executable,
            "browser_profile_dir": self.browser_profile_dir,
            "browser_pid": self.browser_pid,
            "cdp_url": self.cdp_url,
            "requested_url": self.requested_url,
            "first_navigated_url": self.first_navigated_url,
            "final_active_url": self.final_active_url,
            "page_title": self.page_title,
            "navigation_status": self.navigation_status,
            "phase_errors": dict(self.phase_errors),
        }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _allocate_port() -> int:
    """Allocate a free localhost TCP port and return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _research_profile_dir(subdir: str = "research") -> Path:
    """Return the Aura-owned research profile directory, creating it."""
    path = data_dir() / "browse_profiles" / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Browser detection
# ---------------------------------------------------------------------------


def detect_chrome_executable() -> str:
    """Locate a Chrome/Chromium executable on the current platform.

    Raises ``BrowserDetectionError`` when nothing is found.
    """
    is_win = sys.platform == "win32"

    if is_win:
        bases = (
            os.environ.get("ProgramFiles", "C:\\Program Files"),
            os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
            os.environ.get("LocalAppData", ""),
        )
        for base in bases:
            if not base:
                continue
            candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                return str(candidate)

        # PATH search
        for name in ("chrome", "google-chrome"):
            found = _which(name)
            if found:
                return found

        raise BrowserDetectionError("Chrome not found on Windows. Checked Program Files, "
                                    "Program Files (x86), LocalAppData, and PATH.")

    if sys.platform == "darwin":
        candidate = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(candidate):
            return candidate
        raise BrowserDetectionError("Chrome not found at /Applications/Google Chrome.app")

    # Linux
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = _which(name)
        if found:
            return found
    raise BrowserDetectionError("Chrome/Chromium not found on Linux PATH.")


def _which(name: str) -> str | None:
    """Minimal which() that returns None on failure."""
    try:
        import shutil
        return shutil.which(name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------


def _probe_cdp(port: int, timeout: float = _CDP_PROBE_TIMEOUT) -> str | None:
    """Probe ``http://127.0.0.1:<port>/json/version``.

    Returns the ``webSocketDebuggerUrl`` string on success, or ``None``
    if the endpoint is not ready yet.
    """
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        ws_url = data.get("webSocketDebuggerUrl") or ""
        return str(ws_url) if ws_url and ws_url.startswith("ws") else None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError):
        return None


def _wait_for_cdp(port: int, poll_interval: float = _CDP_POLL_INTERVAL,
                  max_wait: float = _CDP_POLL_MAX) -> str:
    """Poll ``/json/version`` until CDP responds with a WebSocket URL.

    Returns the ``webSocketDebuggerUrl``.

    Raises ``CdpReadyTimeout`` on timeout.
    """
    deadline = time.monotonic() + max_wait
    last_error: str = ""
    while time.monotonic() < deadline:
        ws_url = _probe_cdp(port, timeout=_CDP_PROBE_TIMEOUT)
        if ws_url:
            return ws_url
        last_error = f"CDP not ready on port {port}"
        time.sleep(poll_interval)
    raise CdpReadyTimeout(f"CDP did not become ready within {max_wait}s on port {port}. "
                          f"Last probe: {last_error}")


# ---------------------------------------------------------------------------
# Playwright connection helper
# ---------------------------------------------------------------------------

_PLAYWRIGHT_IMPORT_ERROR: str | None = None

try:
    import playwright.sync_api
    _PLAYWRIGHT_IMPORT_ERROR = None
except ImportError as _exc:
    _PLAYWRIGHT_IMPORT_ERROR = str(_exc)
    playwright = None  # type: ignore[assignment]


def _connect_via_playwright(cdp_url: str) -> Any:
    """Connect to an already-running CDP browser and return a Playwright
    ``Browser`` object.

    This lets us use Playwright's page interaction APIs (navigation,
    DOM extraction, JS evaluation) without giving Playwright control
    over browser launch.
    """
    if _PLAYWRIGHT_IMPORT_ERROR:
        raise CdpConnectError(
            f"Playwright is not available: {_PLAYWRIGHT_IMPORT_ERROR}"
        )
    try:
        pw = playwright.sync_api.sync_playwright().start()  # type: ignore[union-attr]
        browser = pw.chromium.connect_over_cdp(cdp_url)
        return pw, browser
    except Exception as exc:
        raise CdpConnectError(f"Failed to connect over CDP: {exc}")


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class ResearchBrowserController:
    """Single owner of browser lifecycle for Aura web research.

    Typical usage::

        ctrl = ResearchBrowserController()
        receipt = ctrl.navigate(url_or_query="latest Python version")
        if receipt.ok:
            page = ctrl.page   # Playwright Page object
            # ... extract content ...
        ctrl.close()

    The controller can also be used as a context manager::

        with ResearchBrowserController() as ctrl:
            receipt = ctrl.navigate("latest Python version")
            ...
    """

    def __init__(self, profile_subdir: str = "research") -> None:
        self._profile_subdir = profile_subdir
        self._process: subprocess.Popen[str] | None = None
        self._port: int | None = None
        self._cdp_url: str | None = None
        self._pw = None  # playwright instance
        self._browser = None  # playwright browser (connected over CDP)
        self._page = None  # current active page
        self._started = False
        self._closed = False
        self._browser_executable: str = ""
        self._profile_path: Path | None = None

    # -- properties ---------------------------------------------------------

    @property
    def page(self) -> Any:
        """The active Playwright ``Page``, or ``None``."""
        return self._page

    @property
    def cdp_url(self) -> str | None:
        """The CDP WebSocket URL, or ``None`` if not connected."""
        return self._cdp_url

    @property
    def browser_pid(self) -> int | None:
        """The browser process PID, or ``None``."""
        if self._process is not None and self._process.poll() is None:
            return self._process.pid
        return None

    @property
    def started(self) -> bool:
        """True once the browser has been launched and connected."""
        return self._started

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> BrowserReceipt:
        """Detect, launch, and connect to the research browser.

        Returns a ``BrowserReceipt`` summarising every step.  The receipt
        has ``.ok == True`` when the browser is ready for navigation.
        """
        receipt = BrowserReceipt()

        # --- detect ---
        try:
            self._browser_executable = detect_chrome_executable()
            receipt.browser_executable = self._browser_executable
        except BrowserDetectionError as exc:
            msg = str(exc)
            receipt.phase_errors["detect"] = msg
            _log.error("research_browser_detect_failed: %s", msg)
            return receipt

        # --- profile dir ---
        try:
            self._profile_path = _research_profile_dir(self._profile_subdir)
            receipt.browser_profile_dir = str(self._profile_path)
        except OSError as exc:
            msg = f"Cannot create profile directory: {exc}"
            receipt.phase_errors["profile_dir"] = msg
            _log.error("research_browser_profile_dir_failed: %s", msg)
            return receipt

        # --- port ---
        try:
            self._port = _allocate_port()
        except OSError as exc:
            msg = f"Cannot allocate free port: {exc}"
            receipt.phase_errors["allocate_port"] = msg
            _log.error("research_browser_port_failed: %s", msg)
            return receipt

        # --- launch ---
        try:
            self._launch()
            receipt.browser_pid = self.browser_pid
        except BrowserLaunchError as exc:
            msg = str(exc)
            receipt.phase_errors["launch"] = msg
            _log.error("research_browser_launch_failed: %s", msg)
            return receipt

        # --- cdp_ready ---
        try:
            ws_url = _wait_for_cdp(self._port)
            self._cdp_url = ws_url
            receipt.cdp_url = ws_url
        except CdpReadyTimeout as exc:
            msg = str(exc)
            receipt.phase_errors["cdp_ready"] = msg
            _log.error("research_browser_cdp_timeout: %s", msg)
            self._kill_process()
            return receipt

        # --- connect ---
        try:
            self._pw, self._browser = _connect_via_playwright(ws_url)
        except CdpConnectError as exc:
            msg = str(exc)
            receipt.phase_errors["connect"] = msg
            _log.error("research_browser_connect_failed: %s", msg)
            self._kill_process()
            return receipt

        # --- page ---
        try:
            self._acquire_page()
        except CdpConnectError as exc:
            msg = str(exc)
            receipt.phase_errors["page"] = msg
            _log.error("research_browser_page_failed: %s", msg)
            self.close()
            return receipt

        self._started = True
        _log.info(
            "research_browser_started executable=%s port=%d profile=%s",
            self._browser_executable,
            self._port,
            self._profile_path,
        )
        return receipt

    def navigate(self, url_or_query: str) -> BrowserReceipt:
        """Navigate the research browser to *url_or_query* and return a receipt.

        If the value does not look like a full URL (no scheme), it is treated
        as a search query and sent to a search engine.
        """
        receipt = self.start()
        if not receipt.ok:
            return receipt

        receipt.requested_url = url_or_query
        target_url = self._resolve_target_url(url_or_query)

        page = self._page
        if page is None:
            receipt.phase_errors["navigate"] = "No active page to navigate."
            receipt.navigation_status = "failed"
            return receipt

        try:
            receipt.first_navigated_url = target_url
            page.goto(target_url, wait_until="domcontentloaded",
                      timeout=_NAVIGATION_TIMEOUT)
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass

            receipt.final_active_url = page.url or target_url
            receipt.page_title = page.title() or ""
            receipt.navigation_status = "success"
        except Exception as exc:
            receipt.phase_errors["navigate"] = str(exc)
            receipt.navigation_status = "failed"
            receipt.final_active_url = page.url if page else target_url
            receipt.page_title = page.title() if page else ""

        return receipt

    def close(self) -> None:
        """Shut down the browser, Playwright connection, and process."""
        if self._closed:
            return
        self._closed = True
        self._started = False

        # Tear down Playwright objects first (they hold CDP connections)
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

        self._kill_process()
        _log.info("research_browser_closed")

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> ResearchBrowserController:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- internal helpers ---------------------------------------------------

    def _launch(self) -> None:
        """Launch the Chrome process with remote debugging."""
        assert self._port is not None
        assert self._profile_path is not None

        args = [
            self._browser_executable,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile_path}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        _log.debug("research_browser_launch cmd=%s", " ".join(args))

        try:
            subprocess_kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                subprocess_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
                si = subprocess.STARTUPINFO()
                si.dwFlags |= 0x00000001  # STARTF_USESHOWWINDOW
                si.wShowWindow = 5  # SW_SHOW
                subprocess_kwargs["startupinfo"] = si

            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_kwargs,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrowserLaunchError(f"Failed to launch browser: {exc}")

        # Quick liveness check
        if self._process.poll() is not None:
            raise BrowserLaunchError(
                f"Browser process exited immediately (code={self._process.returncode})"
            )

    def _kill_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        self._process = None

    def _acquire_page(self) -> None:
        """Get an existing page or create one."""
        if self._browser is None:
            raise CdpConnectError("No browser connected.")
        try:
            contexts = self._browser.contexts
            if contexts:
                pages = contexts[0].pages
                if pages:
                    self._page = pages[0]
                    return
            # No pages exist yet — create one
            if contexts:
                self._page = contexts[0].new_page()
            else:
                ctx = self._browser.new_context(
                    no_viewport=True,
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36")
                )
                self._page = ctx.new_page()
        except Exception as exc:
            raise CdpConnectError(f"Failed to acquire page: {exc}")

    def _resolve_target_url(self, url_or_query: str) -> str:
        """Convert a bare query into a search URL."""
        import urllib.parse
        if url_or_query.startswith(("http://", "https://", "ftp://", "file://")):
            return url_or_query
        encoded = urllib.parse.quote_plus(url_or_query)
        return f"https://www.bing.com/search?q={encoded}"
