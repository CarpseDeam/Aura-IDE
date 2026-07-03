"""Tests for ResearchBrowserController — the single browser owner for web research."""

from __future__ import annotations

import json
import os
import socket
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from aura.browser.research_controller import (
    ResearchBrowserController,
    BrowserReceipt,
    BrowserDetectionError,
    CdpReadyTimeout,
    _allocate_port,
    _research_profile_dir,
    _probe_cdp,
    _wait_for_cdp,
    detect_chrome_executable,
)


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------


class TestAllocatePort:
    def test_returns_available_port(self):
        port = _allocate_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535
        # Verify the port is actually free
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))

    def test_ports_are_unique(self):
        ports = {_allocate_port() for _ in range(5)}
        assert len(ports) == 5  # no duplicates


# ---------------------------------------------------------------------------
# Profile directory
# ---------------------------------------------------------------------------


class TestResearchProfileDir:
    def test_creates_directory(self, tmp_path):
        with patch("aura.browser.research_controller.data_dir", return_value=tmp_path):
            path = _research_profile_dir("test-research")
            assert path.is_dir()
            assert path.name == "test-research"
            assert path.parent.name == "browse_profiles"

    def test_is_idempotent(self, tmp_path):
        with patch("aura.browser.research_controller.data_dir", return_value=tmp_path):
            first = _research_profile_dir("test-research")
            second = _research_profile_dir("test-research")
            assert first == second


# ---------------------------------------------------------------------------
# Chrome detection
# ---------------------------------------------------------------------------


class TestDetectChromeExecutable:
    def test_raises_on_missing(self):
        """When no Chrome is found, detect raises BrowserDetectionError."""
        with patch("aura.browser.research_controller.sys.platform", "linux"):
            with patch("aura.browser.research_controller._which", return_value=None):
                with pytest.raises(BrowserDetectionError, match="Chrome.*not found"):
                    detect_chrome_executable()

    def test_finds_via_which_on_linux(self):
        with patch("aura.browser.research_controller.sys.platform", "linux"):
            with patch("aura.browser.research_controller._which", return_value="/usr/bin/google-chrome"):
                assert detect_chrome_executable() == "/usr/bin/google-chrome"

    def test_checks_windows_program_files(self):
        with patch("aura.browser.research_controller.sys.platform", "win32"):
            fake_exe = str(Path(sys.executable).parent / "chrome.exe")
            # Force the env vars to known paths
            with patch.dict(os.environ, {"ProgramFiles": str(Path(sys.executable).parent)}):
                with patch("aura.browser.research_controller.Path.is_file", return_value=True):
                    path = detect_chrome_executable()
                    assert path.endswith("chrome.exe")


# ---------------------------------------------------------------------------
# CDP probing
# ---------------------------------------------------------------------------


class TestProbeCdp:
    def test_returns_ws_url_when_ready(self):
        """When /json/version returns a webSocketDebuggerUrl, return it."""
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            mock_resp.read.return_value = json.dumps(
                {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"}
            ).encode("utf-8")
            result = _probe_cdp(9222)
            assert result == "ws://127.0.0.1:9222/devtools/page/1"

    def test_returns_none_when_not_ready(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
            assert _probe_cdp(9222) is None

    def test_returns_none_on_bad_json(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            mock_resp.read.return_value = b"not json"
            assert _probe_cdp(9222) is None


class TestWaitForCdp:
    def test_returns_ws_url_when_ready(self):
        with patch("aura.browser.research_controller._probe_cdp", return_value="ws://ready"):
            result = _wait_for_cdp(9222, poll_interval=0.01, max_wait=1.0)
            assert result == "ws://ready"

    def test_raises_on_timeout(self):
        with patch("aura.browser.research_controller._probe_cdp", return_value=None):
            with pytest.raises(CdpReadyTimeout, match="CDP did not become ready"):
                _wait_for_cdp(9222, poll_interval=0.01, max_wait=0.1)


# ---------------------------------------------------------------------------
# Browser receipt
# ---------------------------------------------------------------------------


class TestBrowserReceipt:
    def test_ok_false_when_no_executable(self):
        r = BrowserReceipt()
        assert r.ok is False
        assert r.navigation_status == "not_started"

    def test_ok_true_when_fully_navigated(self):
        r = BrowserReceipt(
            browser_executable="/usr/bin/chrome",
            cdp_url="ws://cdp",
            browser_ready=True,
            navigation_status="success",
        )
        assert r.ok is True
        assert r.navigation_ok is True

    def test_ok_true_when_started_but_not_navigated(self):
        """A plain start() receipt with browser_ready is ok, even before navigation."""
        r = BrowserReceipt(
            browser_executable="/usr/bin/chrome",
            cdp_url="ws://cdp",
            browser_ready=True,
            navigation_status="not_started",
        )
        assert r.ok is True
        assert r.navigation_ok is False

    def test_phase_errors_prevent_ok(self):
        r = BrowserReceipt(phase_errors={"detect": "Chrome not found"})
        assert r.ok is False

    def test_to_dict_round_trip(self):
        r = BrowserReceipt(
            browser_executable="/usr/bin/chrome",
            browser_profile_dir="/tmp/profile",
            cdp_url="ws://127.0.0.1:9222",
            requested_url="test query",
            page_title="Test Page",
            browser_ready=True,
            navigation_status="success",
        )
        d = r.to_dict()
        assert d["controller_version"] == "1.0"
        assert d["browser_ready"] is True
        assert d["navigation_status"] == "success"
        assert d["page_title"] == "Test Page"
        assert d["phase_errors"] == {}


# ---------------------------------------------------------------------------
# Controller lifecycle (mocked)
# ---------------------------------------------------------------------------


class TestResearchBrowserController:
    def test_receipt_on_detect_failure(self):
        """Controller returns a receipt with phase_errors when detection fails."""
        ctrl = ResearchBrowserController()
        with patch("aura.browser.research_controller.detect_chrome_executable",
                   side_effect=BrowserDetectionError("Chrome not found")):
            receipt = ctrl.start()
        assert receipt.ok is False
        assert "detect" in receipt.phase_errors
        assert "Chrome not found" in receipt.phase_errors["detect"]

    def test_receipt_on_launch_failure(self, tmp_path):
        """Controller returns receipt with launch phase error."""
        ctrl = ResearchBrowserController()
        with patch("aura.browser.research_controller.detect_chrome_executable",
                   return_value="/usr/bin/chrome"):
            with patch("aura.browser.research_controller._research_profile_dir",
                       return_value=tmp_path / "research_profile"):
                with patch("aura.browser.research_controller._allocate_port",
                           return_value=9999):
                    with patch("aura.browser.research_controller.subprocess.Popen",
                               side_effect=OSError("Cannot launch")):
                        receipt = ctrl.start()
        assert receipt.ok is False
        assert "launch" in receipt.phase_errors

    def test_receipt_on_cdp_timeout(self, tmp_path):
        """Controller returns receipt with cdp_ready phase error."""
        ctrl = ResearchBrowserController()

        class FakeProc:
            pid = 12345
            returncode = None

            def poll(self):
                return None

        with (
            patch("aura.browser.research_controller.detect_chrome_executable",
                  return_value="/usr/bin/chrome"),
            patch("aura.browser.research_controller._research_profile_dir",
                  return_value=tmp_path / "research_profile"),
            patch("aura.browser.research_controller._allocate_port",
                  return_value=9999),
            patch("aura.browser.research_controller.subprocess.Popen",
                  return_value=FakeProc()),
            patch("aura.browser.research_controller._wait_for_cdp",
                  side_effect=CdpReadyTimeout("CDP timeout")),
        ):
            receipt = ctrl.start()
        assert receipt.ok is False
        assert "cdp_ready" in receipt.phase_errors
        ctrl.close()

    def test_navigate_returns_receipt_early_when_start_fails(self):
        """Navigate returns early when start() fails, but requested_url is set."""
        ctrl = ResearchBrowserController()
        with patch.object(ctrl, "start", return_value=BrowserReceipt()):
            receipt = ctrl.navigate("test query")
        assert receipt.navigation_status == "not_started"
        assert receipt.requested_url == "test query"
        ctrl.close()

    def test_navigate_sets_requested_url_when_start_succeeds(self):
        """Navigate sets requested_url on the receipt when start returns ok.

        Uses a realistic start-style receipt (browser_ready=True,
        navigation_status=not_started) rather than faking navigation
        success from start().
        """
        ctrl = ResearchBrowserController()
        global_nav = {"called": False}

        class FakePage:
            def goto(self, url, **kw):
                global_nav["called"] = True
            def title(self):
                return "Test"
            @property
            def url(self):
                return "https://resolved"

        good_receipt = BrowserReceipt(
            browser_executable="/usr/bin/chrome",
            cdp_url="ws://cdp",
            browser_ready=True,
            navigation_status="not_started",
        )
        ctrl._page = FakePage()
        with patch.object(ctrl, "start", return_value=good_receipt):
            receipt = ctrl.navigate("test query")
        assert receipt.requested_url == "test query"
        assert receipt.navigation_status == "success"
        assert global_nav["called"]

    def test_context_manager_closes(self):
        with ResearchBrowserController() as ctrl:
            assert ctrl._closed is False
        assert ctrl._closed is True

    def test_resolve_target_url_keeps_urls(self):
        ctrl = ResearchBrowserController()
        url = "https://example.com/page"
        resolved = ctrl._resolve_target_url(url)
        assert resolved == url

    def test_resolve_target_url_wraps_queries(self):
        ctrl = ResearchBrowserController()
        resolved = ctrl._resolve_target_url("python version")
        assert "python+version" in resolved
        assert resolved.startswith("https://www.bing.com/search?q=")


# ---------------------------------------------------------------------------
# Regression: start receipt readiness vs navigation status
# ---------------------------------------------------------------------------


class TestStartReceiptReadiness:
    """Verify that a plain start() receipt is usable without fake navigation success."""

    def test_navigate_proceeds_when_start_returns_ready_receipt(self):
        """navigate() should proceed when start() returns browser_ready=True,
        even though navigation_status is still 'not_started'."""
        ctrl = ResearchBrowserController()
        nav_log = {"called": False, "url": ""}

        class FakePage:
            def goto(self, url, **kw):
                nav_log["called"] = True
                nav_log["url"] = url
            def title(self):
                return "Test Results"
            @property
            def url(self):
                return "https://example.com/result"

        ready_receipt = BrowserReceipt(
            browser_executable="/usr/bin/chrome",
            browser_profile_dir="/tmp/profile",
            cdp_url="ws://127.0.0.1:9222",
            browser_pid=12345,
            browser_ready=True,
            navigation_status="not_started",
        )
        ctrl._page = FakePage()
        with patch.object(ctrl, "start", return_value=ready_receipt):
            receipt = ctrl.navigate("test query")
        assert receipt.ok is True
        assert receipt.navigation_ok is True
        assert receipt.navigation_status == "success"
        assert receipt.requested_url == "test query"
        assert nav_log["called"] is True
        assert "bing.com/search" in nav_log["url"]

    def test_navigate_sets_requested_url_even_on_start_failure(self):
        """requested_url should be present on the receipt even when start fails."""
        ctrl = ResearchBrowserController()
        failed_receipt = BrowserReceipt(phase_errors={"detect": "Chrome not found"})
        with patch.object(ctrl, "start", return_value=failed_receipt):
            receipt = ctrl.navigate("test query")
        assert receipt.ok is False
        assert receipt.requested_url == "test query"
        assert receipt.phase_errors.get("detect") == "Chrome not found"


class TestBrowserResearchSessionStart:
    """BrowserResearchSession.start() should accept a ready-but-not-navigated receipt."""

    def _import_session(self):
        """Import BrowserResearchSession from the web-research bundle directory.

        The web-research modules use bare imports (``from models import ...``)
        that require the bundle directory on sys.path.
        """
        import importlib
        bundle_dir = (
            Path(__file__).resolve().parent.parent
            / "aura" / "drones" / "bundled" / "web-research"
        )
        if str(bundle_dir) not in sys.path:
            sys.path.insert(0, str(bundle_dir))
        # Clear any cached imports so the path update takes effect
        for mod in list(sys.modules):
            if "browser_search" in mod or "models" in mod:
                sys.modules.pop(mod, None)
        mod = importlib.import_module("browser_search")
        return mod.BrowserResearchSession

    def test_start_accepts_ready_receipt(self):
        """Session start succeeds when the controller returns browser_ready=True."""
        BrowserResearchSession = self._import_session()

        class FakePage:
            pass

        ready_receipt = BrowserReceipt(
            browser_executable="/usr/bin/chrome",
            browser_profile_dir="/tmp/profile",
            cdp_url="ws://127.0.0.1:9222",
            browser_ready=True,
            navigation_status="not_started",
        )

        # The module is imported as bare "browser_search" (not under the aura
        # package namespace) because the bundle directory is on sys.path.
        with (
            patch("browser_search.ResearchBrowserController") as MockCtrl,
        ):
            instance = MockCtrl.return_value
            instance.start.return_value = ready_receipt
            instance.page = FakePage()

            session = BrowserResearchSession()
            result = session.start()

        assert result is True
        assert session._started is True
        assert session._page is not None
        session.close()


# ---------------------------------------------------------------------------
# Controller launch command shape (mocked)
# ---------------------------------------------------------------------------


class TestControllerLaunchCommand:
    def test_launch_includes_remote_debugging_port(self, tmp_path):
        """The Chrome launch command has --remote-debugging-port."""
        ctrl = ResearchBrowserController()
        ctrl._browser_executable = "/usr/bin/chrome"
        ctrl._profile_path = tmp_path / "profile"
        ctrl._port = 9222

        cmd = [
            ctrl._browser_executable,
            f"--remote-debugging-port={ctrl._port}",
            f"--user-data-dir={ctrl._profile_path}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        assert "--remote-debugging-port=9222" in cmd
        assert f"--user-data-dir={ctrl._profile_path}" in cmd
        assert "--no-first-run" in cmd
        assert "--no-default-browser-check" in cmd

    def test_launch_includes_research_profile(self, tmp_path):
        """The launch command uses the Aura research profile directory."""
        ctrl = ResearchBrowserController()
        ctrl._browser_executable = "/usr/bin/chrome"
        ctrl._profile_path = tmp_path / "research_profile"
        ctrl._port = 9223

        cmd = [
            ctrl._browser_executable,
            f"--remote-debugging-port={ctrl._port}",
            f"--user-data-dir={ctrl._profile_path}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        assert f"--user-data-dir={tmp_path / 'research_profile'}" in cmd
