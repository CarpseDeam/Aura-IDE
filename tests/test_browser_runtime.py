"""Tests for BrowserRuntime browser detection and launch routing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from aura.browser.runtime import BrowserChoice, BrowserRuntime, _detect_installed_browsers

# =========================================================================
# _detect_installed_browsers — Windows detection logic
# =========================================================================


class TestDetectInstalledBrowsersWindows:
    """Windows browser detection via _detect_installed_browsers()."""

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_chrome_detected_before_edge(self) -> None:
        """Chrome found via ProgramFiles path, Edge also found.  First
        choice is Chrome, second is Edge."""
        path_pf = "C:\\Program Files"
        path_pf86 = "C:\\Program Files (x86)"
        path_la = "C:\\Users\\user\\AppData\\Local"

        chrome_exe = f"{path_pf}\\Google\\Chrome\\Application\\chrome.exe"
        edge_exe = f"{path_pf86}\\Microsoft\\Edge\\Application\\msedge.exe"

        def isfile_side_effect(p: str) -> bool:
            return p in (chrome_exe, edge_exe)

        with patch("aura.browser.runtime.os.environ.get") as mock_env_get:
            mock_env_get.side_effect = lambda key, default=None: {
                "ProgramFiles": path_pf,
                "ProgramFiles(x86)": path_pf86,
                "LocalAppData": path_la,
            }.get(key, default)

            with patch("aura.browser.runtime.os.path.isfile", side_effect=isfile_side_effect):
                with patch("aura.browser.runtime.shutil.which", return_value=None):
                    choices = _detect_installed_browsers()

        assert len(choices) >= 2
        assert choices[0].id == "chrome"
        assert choices[1].id == "edge"

    @patch("sys.platform", "win32")
    def test_edge_detected_when_chrome_absent(self) -> None:
        """All Chrome paths missing, Edge present.  First choice is Edge."""
        path_pf = "C:\\Program Files"
        path_pf86 = "C:\\Program Files (x86)"
        path_la = "C:\\Users\\user\\AppData\\Local"

        edge_exe = f"{path_pf86}\\Microsoft\\Edge\\Application\\msedge.exe"

        def isfile_side_effect(p: str) -> bool:
            return p == edge_exe

        with patch("aura.browser.runtime.os.environ.get") as mock_env_get:
            mock_env_get.side_effect = lambda key, default=None: {
                "ProgramFiles": path_pf,
                "ProgramFiles(x86)": path_pf86,
                "LocalAppData": path_la,
            }.get(key, default)

            with patch("aura.browser.runtime.os.path.isfile", side_effect=isfile_side_effect):
                with patch("aura.browser.runtime.shutil.which", return_value=None):
                    choices = _detect_installed_browsers()

        assert len(choices) >= 1
        assert choices[0].id == "edge"

    @patch("sys.platform", "win32")
    def test_brave_detected_after_chrome_and_edge(self) -> None:
        """Chrome, Edge, and Brave all found.  Brave appears at index 2
        (after Chrome and Edge, before Chromium fallback)."""
        path_pf = "C:\\Program Files"
        path_pf86 = "C:\\Program Files (x86)"
        path_la = "C:\\Users\\user\\AppData\\Local"

        chrome_exe = f"{path_pf}\\Google\\Chrome\\Application\\chrome.exe"
        edge_exe = f"{path_pf86}\\Microsoft\\Edge\\Application\\msedge.exe"
        brave_exe = f"{path_la}\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"

        def isfile_side_effect(p: str) -> bool:
            return p in (chrome_exe, edge_exe, brave_exe)

        with patch("aura.browser.runtime.os.environ.get") as mock_env_get:
            mock_env_get.side_effect = lambda key, default=None: {
                "ProgramFiles": path_pf,
                "ProgramFiles(x86)": path_pf86,
                "LocalAppData": path_la,
            }.get(key, default)

            with patch("aura.browser.runtime.os.path.isfile", side_effect=isfile_side_effect):
                with patch("aura.browser.runtime.shutil.which", return_value=None):
                    choices = _detect_installed_browsers()

        assert len(choices) >= 4
        assert choices[0].id == "chrome"
        assert choices[1].id == "edge"
        assert choices[2].id == "brave"
        # Brave uses executable_path, no channel
        assert choices[2].channel is None
        assert choices[2].executable_path == brave_exe

    # ------------------------------------------------------------------
    # Fallback only
    # ------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_no_installed_browser_returns_only_fallback(self) -> None:
        """All env-var paths missing and shutil.which returns None.
        Only choice should be the Playwright Chromium fallback."""
        with patch("aura.browser.runtime.os.environ.get") as mock_env_get:
            mock_env_get.side_effect = lambda key, default=None: {
                "ProgramFiles": "C:\\Program Files",
                "ProgramFiles(x86)": "C:\\Program Files (x86)",
                "LocalAppData": "C:\\Users\\user\\AppData\\Local",
            }.get(key, default)

            with patch("aura.browser.runtime.os.path.isfile", return_value=False):
                with patch("aura.browser.runtime.shutil.which", return_value=None):
                    choices = _detect_installed_browsers()

        assert len(choices) == 1
        assert choices[0].id == "chromium"
        assert choices[0].source == "playwright"

    @patch("sys.platform", "win32")
    def test_chromium_fallback_always_last(self) -> None:
        """Regardless of what is detected, last choice is always the
        Playwright Chromium fallback."""
        path_pf = "C:\\Program Files"
        chrome_exe = f"{path_pf}\\Google\\Chrome\\Application\\chrome.exe"

        def isfile_side_effect(p: str) -> bool:
            return p == chrome_exe

        with patch("aura.browser.runtime.os.environ.get") as mock_env_get:
            mock_env_get.side_effect = lambda key, default=None: {
                "ProgramFiles": path_pf,
                "ProgramFiles(x86)": "C:\\Program Files (x86)",
                "LocalAppData": "C:\\Users\\user\\AppData\\Local",
            }.get(key, default)

            with patch("aura.browser.runtime.os.path.isfile", side_effect=isfile_side_effect):
                with patch("aura.browser.runtime.shutil.which", return_value=None):
                    choices = _detect_installed_browsers()

        assert choices[-1].id == "chromium"
        assert choices[-1].source == "playwright"

    @patch("sys.platform", "win32")
    def test_shutil_which_fallback_works(self) -> None:
        """No env-var paths present for Chrome, but shutil.which returns
        a path.  Chrome is detected with source='installed'."""
        with patch("aura.browser.runtime.os.environ.get") as mock_env_get:
            mock_env_get.side_effect = lambda key, default=None: {
                "ProgramFiles": "C:\\Program Files",
                "ProgramFiles(x86)": "C:\\Program Files (x86)",
                "LocalAppData": "C:\\Users\\user\\AppData\\Local",
            }.get(key, default)

            with patch("aura.browser.runtime.os.path.isfile", return_value=False):
                with patch("aura.browser.runtime.shutil.which", return_value="C:\\chrome.exe"):
                    choices = _detect_installed_browsers()

        assert choices[0].id == "chrome"
        assert choices[0].source == "installed"


# =========================================================================
# BrowserRuntime.start() — launch routing
# =========================================================================


class TestBrowserRuntimeLaunchRouting:
    """BrowserRuntime.start() routing logic with mocked Playwright."""

    @staticmethod
    def _make_mock_playwright():
        """Build a mock Playwright sync_api fixture."""
        mock_pw = MagicMock()
        # sync_playwright() returns mock_pw; .start() must return the same
        mock_pw.start.return_value = mock_pw
        mock_chromium = MagicMock()
        mock_pw.chromium = mock_chromium
        mock_context = MagicMock()
        mock_chromium.launch_persistent_context.return_value = mock_context
        mock_browser = MagicMock()
        mock_chromium.launch.return_value = mock_browser
        mock_sync_playwright = MagicMock(return_value=mock_pw)
        return mock_pw, mock_chromium, mock_context, mock_browser, mock_sync_playwright

    # ------------------------------------------------------------------
    # Persistent mode
    # ------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_persistent_mode_passes_user_data_dir_and_channel(self) -> None:
        """Persistent (user_data_dir) mode calls launch_persistent_context
        with proper args."""
        mock_pw, mock_chromium, mock_context, _, mock_sync = self._make_mock_playwright()

        with patch("playwright.sync_api.sync_playwright", mock_sync):
            with patch(
                "aura.browser.runtime._detect_installed_browsers",
                return_value=[
                    BrowserChoice(
                        id="chrome",
                        label="Google Chrome",
                        channel="chrome",
                        executable_path=None,
                        source="installed",
                    ),
                    BrowserChoice(
                        id="chromium",
                        label="Chromium",
                        channel=None,
                        executable_path=None,
                        source="playwright",
                    ),
                ],
            ):
                runtime = BrowserRuntime(
                    headless=True, user_data_dir=Path("/tmp/test-profile")
                )
                result = runtime.start()

        assert result is True
        mock_chromium.launch_persistent_context.assert_called_once_with(
            user_data_dir=str(Path("/tmp/test-profile")),
            channel="chrome",
            headless=True,
        )

    # ------------------------------------------------------------------
    # Anonymous mode
    # ------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_anonymous_mode_passes_channel_to_launch(self) -> None:
        """Anonymous (no user_data_dir) mode calls launch with channel
        and then new_context."""
        mock_pw, mock_chromium, mock_context, mock_browser, mock_sync = (
            self._make_mock_playwright()
        )

        with patch("playwright.sync_api.sync_playwright", mock_sync):
            with patch(
                "aura.browser.runtime._detect_installed_browsers",
                return_value=[
                    BrowserChoice(
                        id="chrome",
                        label="Google Chrome",
                        channel="chrome",
                        executable_path=None,
                        source="installed",
                    ),
                    BrowserChoice(
                        id="chromium",
                        label="Chromium",
                        channel=None,
                        executable_path=None,
                        source="playwright",
                    ),
                ],
            ):
                runtime = BrowserRuntime(headless=False)
                result = runtime.start()

        assert result is True
        mock_chromium.launch.assert_called_once_with(
            channel="chrome", headless=False
        )
        mock_browser.new_context.assert_called_once()

    # ------------------------------------------------------------------
    # Brave uses executable_path
    # ------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_brave_uses_executable_path_not_channel(self) -> None:
        """Brave detection uses executable_path, not channel."""
        mock_pw, mock_chromium, mock_context, mock_browser, mock_sync = (
            self._make_mock_playwright()
        )
        brave_exe = "C:\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"

        with patch("playwright.sync_api.sync_playwright", mock_sync):
            with patch(
                "aura.browser.runtime._detect_installed_browsers",
                return_value=[
                    BrowserChoice(
                        id="brave",
                        label="Brave",
                        channel=None,
                        executable_path=brave_exe,
                        source="installed",
                    ),
                ],
            ):
                runtime = BrowserRuntime(headless=True)
                result = runtime.start()

        assert result is True
        mock_chromium.launch.assert_called_once_with(
            executable_path=brave_exe, headless=True
        )
        # No channel kwarg should be present
        call_kwargs = mock_chromium.launch.call_args.kwargs
        assert "channel" not in call_kwargs

    # ------------------------------------------------------------------
    # Fall-through behaviour
    # ------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_failed_chrome_falls_through_to_edge(self) -> None:
        """First route (Chrome) raises, second (Edge) succeeds.
        route_metadata reflects Edge."""
        mock_pw, mock_chromium, mock_context, mock_browser, mock_sync = (
            self._make_mock_playwright()
        )
        # First launch attempt raises, second succeeds (anonymous mode uses launch())
        mock_chromium.launch.side_effect = [
            RuntimeError("Chrome failed"),
            mock_browser,
        ]

        with patch("playwright.sync_api.sync_playwright", mock_sync):
            with patch(
                "aura.browser.runtime._detect_installed_browsers",
                return_value=[
                    BrowserChoice(
                        id="chrome",
                        label="Google Chrome",
                        channel="chrome",
                        executable_path=None,
                        source="installed",
                    ),
                    BrowserChoice(
                        id="edge",
                        label="Microsoft Edge",
                        channel="msedge",
                        executable_path=None,
                        source="installed",
                    ),
                ],
            ):
                runtime = BrowserRuntime(headless=True)
                result = runtime.start()

        assert result is True
        meta = runtime.route_metadata
        assert meta["browser_id"] == "edge"
        assert "chrome" in meta["attempted_routes"]

    @patch("sys.platform", "win32")
    def test_all_routes_failed_returns_false(self) -> None:
        """All launch attempts raise.  start() returns False."""
        mock_pw, mock_chromium, mock_context, mock_browser, mock_sync = (
            self._make_mock_playwright()
        )
        mock_chromium.launch.side_effect = RuntimeError("fail")

        with patch("playwright.sync_api.sync_playwright", mock_sync):
            with patch(
                "aura.browser.runtime._detect_installed_browsers",
                return_value=[
                    BrowserChoice(
                        id="chrome",
                        label="Google Chrome",
                        channel="chrome",
                        executable_path=None,
                        source="installed",
                    ),
                ],
            ):
                runtime = BrowserRuntime(headless=True)
                result = runtime.start()

        assert result is False
        assert "All browser routes failed" in (runtime.unavailable_reason or "")

    # ------------------------------------------------------------------
    # route_metadata after success
    # ------------------------------------------------------------------

    @patch("sys.platform", "win32")
    def test_success_sets_route_metadata(self) -> None:
        """After successful start, route_metadata has all expected keys."""
        mock_pw, mock_chromium, mock_context, mock_browser, mock_sync = (
            self._make_mock_playwright()
        )

        with patch("playwright.sync_api.sync_playwright", mock_sync):
            with patch(
                "aura.browser.runtime._detect_installed_browsers",
                return_value=[
                    BrowserChoice(
                        id="chrome",
                        label="Google Chrome",
                        channel="chrome",
                        executable_path=None,
                        source="installed",
                    ),
                ],
            ):
                runtime = BrowserRuntime(headless=True)
                result = runtime.start()

        assert result is True
        meta = runtime.route_metadata
        assert meta["browser_id"] == "chrome"
        assert meta["browser_label"] == "Google Chrome"
        assert meta["browser_source"] == "installed"
        assert meta["browser_persistent"] is False
        assert meta["browser_visible"] is False  # headless=True
        assert isinstance(meta["attempted_routes"], list)

    @patch("sys.platform", "win32")
    def test_success_metadata_with_persistent_profile(self) -> None:
        """Persistent profile sets browser_persistent=True."""
        mock_pw, mock_chromium, mock_context, _, mock_sync = self._make_mock_playwright()

        with patch("playwright.sync_api.sync_playwright", mock_sync):
            with patch(
                "aura.browser.runtime._detect_installed_browsers",
                return_value=[
                    BrowserChoice(
                        id="chrome",
                        label="Google Chrome",
                        channel="chrome",
                        executable_path=None,
                        source="installed",
                    ),
                ],
            ):
                runtime = BrowserRuntime(
                    headless=False, user_data_dir=Path("/tmp/persistent")
                )
                result = runtime.start()

        assert result is True
        meta = runtime.route_metadata
        assert meta["browser_persistent"] is True
        assert meta["browser_visible"] is True
