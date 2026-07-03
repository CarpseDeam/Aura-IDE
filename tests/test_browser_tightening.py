"""Focused tests for receipt/policy tightening pass.

Covers: reuse receipt fields, fresh start receipt fields, anti-blank-slab
page acquisition, observe partial-status honesty, and session sync.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.browser.receipts import BrowserReceipt, BrowserSession
from aura.browser.research_controller import ResearchBrowserController
from aura.browser.browser_service import AuraBrowserService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_page(url: str = "", title: str = "", text: str = "") -> MagicMock:
    p = MagicMock()
    p.url = url
    p.title.return_value = title
    p.locator.return_value.inner_text.return_value = text
    return p


def _ready_receipt() -> BrowserReceipt:
    return BrowserReceipt(
        browser_executable="/usr/bin/chrome",
        browser_profile_dir="/tmp/profile",
        browser_pid=12345,
        cdp_url="ws://127.0.0.1:9222",
        browser_ready=True,
        navigation_status="not_started",
        session_id="sess-1",
        started_at="2025-01-01T00:00:00",
    )


# ---------------------------------------------------------------------------
# 1. Reused start receipt
# ---------------------------------------------------------------------------


class TestReusedStartReceipt:
    def test_has_operation_phase_reuse_all_fields(self):
        """Reused receipt carries operation="start", phase="reuse",
        session_id, reused_existing=True, page metadata, and identity fields."""
        ctrl = ResearchBrowserController()
        ctrl._browser_executable = "/usr/bin/chrome"
        ctrl._profile_path = Path("/tmp/profile")
        ctrl._cdp_url = "ws://127.0.0.1:9999"
        ctrl._started = True
        p = _fake_page("https://example.com", "Example Page")
        ctrl._page = p
        ctrl._browser = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.pages = [p]
        ctrl._browser.contexts = [fake_ctx]

        r = ctrl.start()

        assert r.operation == "start"
        assert r.phase == "reuse"
        assert r.reused_existing is True
        assert r.browser_ready is True
        assert r.navigation_status == "not_started"
        assert r.session_id == ctrl._session.session_id
        assert r.started_at == ctrl._session.started_at
        assert r.browser_executable == "/usr/bin/chrome"
        assert r.browser_profile_dir == str(ctrl._profile_path)
        assert r.cdp_url == "ws://127.0.0.1:9999"
        assert r.page_count == 1
        assert r.page_index == 0
        assert r.final_active_url == "https://example.com"
        assert r.page_title == "Example Page"
        assert r.ok is True

    def test_syncs_before_returning(self):
        """_sync_session_state is called so page_index and page_count are fresh."""
        ctrl = ResearchBrowserController()
        ctrl._browser_executable = "/usr/bin/chrome"
        ctrl._profile_path = Path("/tmp/profile")
        ctrl._cdp_url = "ws://127.0.0.1:9999"
        ctrl._started = True
        p0 = _fake_page("https://a.com", "A")
        p1 = _fake_page("https://b.com", "B")
        ctrl._page = p1
        ctrl._browser = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.pages = [p0, p1]
        ctrl._browser.contexts = [fake_ctx]

        r = ctrl.start()

        assert r.page_count == 2
        assert r.page_index == 1
        assert r.final_active_url == "https://b.com"
        assert r.page_title == "B"


# ---------------------------------------------------------------------------
# 2. Fresh start receipt
# ---------------------------------------------------------------------------


class TestFreshStartReceipt:
    def test_includes_session_id_and_operation(self, tmp_path):
        """Fresh start receipt carries operation="start", phase="complete",
        session_id, and started_at."""

        class FakeProc:
            pid = 12345

            def poll(self):
                return None

        ctrl = ResearchBrowserController()
        new_p = _fake_page("about:blank", "")

        with (
            patch("aura.browser.research_controller.detect_chrome_executable",
                  return_value="/usr/bin/chrome"),
            patch("aura.browser.research_controller._research_profile_dir",
                  return_value=tmp_path / "profile"),
            patch("aura.browser.research_controller._allocate_port",
                  return_value=9999),
            patch("aura.browser.research_controller.subprocess.Popen",
                  return_value=FakeProc()),
            patch("aura.browser.research_controller._wait_for_cdp",
                  return_value="ws://127.0.0.1:9999"),
        ):
            ctrl._browser = MagicMock()
            fake_ctx = MagicMock()
            fake_ctx.new_page.return_value = new_p
            fake_ctx.pages = [new_p]
            ctrl._browser.contexts = [fake_ctx]
            ctrl._pw = MagicMock()

            with patch("aura.browser.research_controller._connect_via_playwright",
                       return_value=(ctrl._pw, ctrl._browser)):
                r = ctrl.start()

        assert r.operation == "start"
        assert r.phase == "complete"
        assert r.session_id != ""
        assert r.started_at != ""
        assert r.browser_ready is True
        assert r.navigation_status == "not_started"
        assert r.browser_executable == "/usr/bin/chrome"
        assert r.ok is True

        ctrl.close()


# ---------------------------------------------------------------------------
# 3. Anti-blank-slab page acquisition
# ---------------------------------------------------------------------------


class TestAcquirePagePolicy:
    @staticmethod
    def _make_ctrl(*pages: MagicMock) -> ResearchBrowserController:
        ctrl = ResearchBrowserController()
        ctrl._browser = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.pages = list(pages)
        ctrl._browser.contexts = [fake_ctx]
        return ctrl

    def test_keeps_current_usable_page(self):
        ctrl = self._make_ctrl()
        kept = _fake_page("https://keep-me.com", "Keep")
        ctrl._page = kept
        ctrl._acquire_page()
        assert ctrl._page is kept

    def test_prefers_non_blank_over_blank(self):
        blank = _fake_page("about:blank", "")
        good = _fake_page("https://example.com", "Example")
        ctrl = self._make_ctrl(blank, good)
        ctrl._page = None
        ctrl._acquire_page()
        assert ctrl._page is good

    def test_falls_back_to_blank_when_only_blank_exists(self):
        blank = _fake_page("about:blank", "")
        ctrl = self._make_ctrl(blank)
        ctrl._page = None
        ctrl._browser.contexts[0].new_page = MagicMock()
        ctrl._acquire_page()
        assert ctrl._page is blank
        ctrl._browser.contexts[0].new_page.assert_not_called()

    def test_creates_new_page_only_when_no_pages_exist(self):
        ctrl = self._make_ctrl()  # empty pages
        ctrl._page = None
        new_p = _fake_page("about:blank", "")
        ctrl._browser.contexts[0].new_page.return_value = new_p
        ctrl._acquire_page()
        assert ctrl._page is new_p
        ctrl._browser.contexts[0].new_page.assert_called_once()

    def test_creates_new_context_when_none_exist(self):
        ctrl = ResearchBrowserController()
        ctrl._browser = MagicMock()
        ctrl._browser.contexts = []
        ctrl._page = None
        new_ctx = MagicMock()
        new_p = _fake_page("about:blank", "")
        new_ctx.new_page.return_value = new_p
        ctrl._browser.new_context.return_value = new_ctx
        ctrl._acquire_page()
        ctrl._browser.new_context.assert_called_once()
        new_ctx.new_page.assert_called_once()
        assert ctrl._page is new_p

    def test_treats_chrome_newtab_as_blank(self):
        chrome_blank = _fake_page("chrome://newtab", "New Tab")
        good = _fake_page("https://example.com", "Example")
        ctrl = self._make_ctrl(chrome_blank, good)
        ctrl._page = None
        ctrl._acquire_page()
        assert ctrl._page is good

    def test_never_creates_when_usable_tab_exists(self):
        blank = _fake_page("", "")
        ctrl = self._make_ctrl(blank)
        ctrl._page = None
        ctrl._browser.contexts[0].new_page = MagicMock()
        ctrl._acquire_page()
        assert ctrl._page is blank
        ctrl._browser.contexts[0].new_page.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Session sync — page index and count
# ---------------------------------------------------------------------------


class TestSessionSync:
    def test_updates_active_page_index_and_page_count(self):
        ctrl = ResearchBrowserController()
        p0 = _fake_page("https://a.com", "A")
        p1 = _fake_page("https://b.com", "B")
        p2 = _fake_page("https://c.com", "C")
        ctrl._page = p1
        ctrl._browser = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.pages = [p0, p1, p2]
        ctrl._browser.contexts = [fake_ctx]
        ctrl._started = True
        ctrl._cdp_url = "ws://cdp"

        ctrl._sync_session_state()

        assert ctrl._session.page_count == 3
        assert ctrl._session.active_page_index == 1
        assert ctrl._session.active_page_url == "https://b.com"
        assert ctrl._session.active_page_title == "B"

    def test_active_page_index_minus_one_when_not_in_list(self):
        p0 = _fake_page("https://a.com", "A")
        orphan = _fake_page("https://orphan.com", "Orphan")
        ctrl = ResearchBrowserController()
        ctrl._page = orphan
        ctrl._browser = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.pages = [p0]
        ctrl._browser.contexts = [fake_ctx]

        ctrl._sync_session_state()

        assert ctrl._session.page_count == 1
        assert ctrl._session.active_page_index == -1

    def test_page_count_zero_when_no_contexts(self):
        ctrl = ResearchBrowserController()
        ctrl._page = _fake_page("https://a.com", "A")
        ctrl._browser = MagicMock()
        ctrl._browser.contexts = []

        ctrl._sync_session_state()

        assert ctrl._session.page_count == 0
        assert ctrl._session.active_page_index == -1


# ---------------------------------------------------------------------------
# 5. Observe partial status not overwritten
# ---------------------------------------------------------------------------


class TestObservePartialStatus:
    def test_partial_status_not_overwritten_to_success(self):
        """When observe() sets observation_status='partial', _ok() must
        not overwrite it to 'success'."""
        svc = AuraBrowserService()
        page = MagicMock()
        page.url = "https://example.com"
        page.title.return_value = "Example"
        # _visible_text will fail; _visible_links succeeds
        svc._ctrl._page = page
        svc._ctrl._started = True
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = []

        with patch.object(svc, "_visible_text", side_effect=RuntimeError("timeout")):
            r = svc.observe()

        assert r.observation_status == "partial"
        assert "observe_text" in r.phase_errors
        assert r.final_active_url == "https://example.com"
        assert r.page_title == "Example"
        assert "links" in r.metadata
        assert r.ok is False  # phase_errors present

    def test_partial_on_text_failure_still_gets_title_and_links(self):
        """Even when visible_text fails, URL, title, and links are captured."""
        svc = AuraBrowserService()
        page = MagicMock()
        page.url = "https://example.com"
        page.title.return_value = "Test"
        svc._ctrl._page = page
        svc._ctrl._started = True
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = []

        with patch.object(svc, "_visible_text", side_effect=RuntimeError("crash")):
            r = svc.observe()

        assert r.observation_status == "partial"
        assert r.final_active_url == "https://example.com"
        assert r.page_title == "Test"
        assert r.ok is False

    def test_success_when_all_phases_succeed(self):
        svc = AuraBrowserService()
        page = _fake_page("https://example.com", "Example", "Hello world.")
        svc._ctrl._page = page
        svc._ctrl._started = True
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [MagicMock(pages=[page])]

        r = svc.observe()

        assert r.observation_status == "success"
        assert not r.phase_errors
        assert r.ok is True
