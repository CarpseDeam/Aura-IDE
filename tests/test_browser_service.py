"""Tests for AuraBrowserService — the extended browser API layer."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aura.browser.receipts import BrowserReceipt, BrowserSession, PageInfo
from aura.browser.research_controller import ResearchBrowserController
from aura.browser.browser_service import AuraBrowserService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _fake_page(url: str = "https://example.com", title: str = "Example",
               text: str = "Hello world.") -> MagicMock:
    p = MagicMock()
    p.url = url
    p.title.return_value = title
    p.locator.return_value.inner_text.return_value = text
    return p


# ---------------------------------------------------------------------------
# Service properties and lifecycle
# ---------------------------------------------------------------------------


class TestServiceLifecycle:
    def test_start_delegates_to_controller(self):
        ctrl = ResearchBrowserController()
        with patch.object(ctrl, "start", return_value=_ready_receipt()):
            svc = AuraBrowserService()
            svc._ctrl = ctrl
            r = svc.start()
            assert r.ok is True
            assert r.browser_ready is True

    def test_close_delegates_to_controller(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        svc._ctrl._closed = False
        svc.close()
        assert svc._ctrl._closed is True

    def test_context_manager_closes(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        with svc:
            pass
        assert svc._ctrl._closed is True

    def test_session_property_returns_snapshot(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        svc._ctrl._session = BrowserSession(session_id="test-1")
        s = svc.session
        assert s.session_id == "test-1"

    def test_search_url_pattern_property(self):
        svc = AuraBrowserService(search_url_pattern="https://google.com/search?q={query}")
        assert "google.com" in svc.search_url_pattern


# ---------------------------------------------------------------------------
# Page / tab management
# ---------------------------------------------------------------------------


class TestPageManagement:
    def test_list_pages_empty_when_no_browser(self):
        svc = AuraBrowserService()
        assert svc.list_pages() == []

    def test_list_pages_with_fake_pages(self):
        svc = AuraBrowserService()
        fake_ctx = MagicMock()
        p1 = _fake_page("https://a.com", "A")
        p2 = _fake_page("https://b.com", "B")
        fake_ctx.pages = [p1, p2]
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [fake_ctx]
        svc._ctrl._page = p1

        pages = svc.list_pages()
        assert len(pages) == 2
        assert pages[0].url == "https://a.com"
        assert pages[0].is_active is True
        assert pages[1].url == "https://b.com"
        assert pages[1].is_active is False

    def test_acquire_active_page_delegates(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        svc._ctrl._page = _fake_page()
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = []

        with patch.object(svc._ctrl, "_acquire_page") as mock_acq:
            r = svc.acquire_active_page()
        mock_acq.assert_called_once()
        assert r.observation_status == "success"

    def test_acquire_active_page_fails_when_not_started(self):
        svc = AuraBrowserService()
        r = svc.acquire_active_page()
        assert r.observation_status == "failed"
        assert "acquire" in r.phase_errors

    def test_create_tab_creates_new_page(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        new_page = _fake_page("about:blank", "")
        fake_ctx = MagicMock()
        fake_ctx.new_page.return_value = new_page
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [fake_ctx]
        svc._ctrl._page = new_page

        r = svc.create_tab()
        assert r.action_status == "success"
        assert r.browser_ready is True

    def test_create_tab_with_url_navigates(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        new_page = _fake_page()
        fake_ctx = MagicMock()
        fake_ctx.new_page.return_value = new_page
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [fake_ctx]
        svc._ctrl._page = new_page

        r = svc.create_tab(url="https://target.com")
        assert r.requested_target == "https://target.com"
        new_page.goto.assert_called_once()

    def test_switch_tab_by_index(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        p0, p1 = _fake_page("https://a.com", "A"), _fake_page("https://b.com", "B")
        fake_ctx = MagicMock()
        fake_ctx.pages = [p0, p1]
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [fake_ctx]
        svc._ctrl._page = p0

        r = svc.switch_tab(1)
        assert r.action_status == "success"
        p1.bring_to_front.assert_called_once()
        assert svc._ctrl._page is p1

    def test_switch_tab_out_of_range(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [MagicMock(pages=[])]

        r = svc.switch_tab(5)
        assert r.action_status == "failed"
        assert "out of range" in r.phase_errors["switch"]

    def test_close_current_tab(self):
        svc = AuraBrowserService()
        svc._ctrl._started = True
        p0 = _fake_page("https://a.com", "A")
        fake_ctx = MagicMock()
        fake_ctx.pages = [p0]
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [fake_ctx]
        svc._ctrl._page = p0

        r = svc.close_current_tab()
        assert r.action_status == "success"
        p0.close.assert_called_once()


# ---------------------------------------------------------------------------
# Navigation API
# ---------------------------------------------------------------------------


class TestNavigationAPI:
    def test_goto_url_navigates(self):
        svc = AuraBrowserService()
        page = _fake_page()
        svc._ctrl._page = page
        svc._ctrl._started = True
        with patch.object(svc._ctrl, "start", return_value=_ready_receipt()):
            r = svc.goto_url("https://example.com")
        assert r.navigation_status == "success"
        assert r.requested_target == "https://example.com"
        page.goto.assert_called_once()

    def test_goto_url_sets_requested_fields(self):
        svc = AuraBrowserService()
        page = _fake_page()
        svc._ctrl._page = page
        svc._ctrl._started = True
        with patch.object(svc._ctrl, "start", return_value=_ready_receipt()):
            r = svc.goto_url("https://example.com/page")
        assert r.requested_target == "https://example.com/page"
        assert r.first_navigated_url == "https://example.com/page"
        assert r.final_active_url == "https://example.com"

    def test_search_resolves_through_configured_pattern(self):
        svc = AuraBrowserService(search_url_pattern="https://www.google.com/search?q={query}")
        page = _fake_page()
        svc._ctrl._page = page
        svc._ctrl._started = True
        with patch.object(svc._ctrl, "start", return_value=_ready_receipt()):
            r = svc.search("python version")
        assert r.operation == "search"
        assert r.requested_target == "python version"
        assert "google.com/search" in r.metadata["search_url"]
        assert "python+version" in r.metadata["search_url"]

    def test_reload(self):
        svc = AuraBrowserService()
        page = _fake_page()
        svc._ctrl._page = page
        svc._ctrl._started = True
        r = svc.reload()
        assert r.navigation_status == "success"
        page.reload.assert_called_once()

    def test_reload_fails_when_no_page(self):
        svc = AuraBrowserService()
        r = svc.reload()
        assert r.navigation_status == "failed"

    def test_navigate_is_backward_compatible(self):
        svc = AuraBrowserService()
        page = _fake_page()
        svc._ctrl._page = page
        with patch.object(svc._ctrl, "start", return_value=_ready_receipt()):
            r = svc.navigate("python version")
        assert r.navigation_status == "success"


# ---------------------------------------------------------------------------
# Observation API
# ---------------------------------------------------------------------------


class TestObservationAPI:
    def test_observe_returns_facts(self):
        svc = AuraBrowserService()
        page = _fake_page("https://example.com", "Example Page", "Visible content here.")
        svc._ctrl._page = page
        svc._ctrl._started = True
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [MagicMock(pages=[page])]

        r = svc.observe()
        assert r.observation_status == "success"
        assert r.final_active_url == "https://example.com"
        assert r.page_title == "Example Page"
        assert "Visible content here." in r.metadata.get("visible_text", "")

    def test_extract_visible_text(self):
        svc = AuraBrowserService()
        page = _fake_page(text="Some body text.")
        svc._ctrl._page = page
        svc._ctrl._started = True

        r = svc.extract_visible_text()
        assert r.observation_status == "success"
        assert "Some body text." in r.metadata["visible_text"]

    def test_extract_links_with_fake_page(self):
        svc = AuraBrowserService()
        page = MagicMock()
        page.url = "https://example.com"
        page.title.return_value = "Links Page"
        svc._ctrl._page = page
        svc._ctrl._started = True
        svc._ctrl._browser = MagicMock()
        svc._ctrl._browser.contexts = [MagicMock(pages=[page])]

        with patch.object(svc, "_visible_links", return_value=[
            {"text": "Link One", "href": "https://one.com"},
            {"text": "Link Two", "href": "https://two.com"},
        ]):
            r = svc.extract_links()
        assert r.observation_status == "success"
        assert r.metadata["link_count"] == 2
        assert r.metadata["links"][0]["text"] == "Link One"

    def test_observe_fails_when_not_started(self):
        svc = AuraBrowserService()
        r = svc.observe()
        assert r.observation_status == "failed"

    def test_extract_text_fails_when_no_page(self):
        svc = AuraBrowserService()
        r = svc.extract_visible_text()
        assert r.observation_status == "failed"


# ---------------------------------------------------------------------------
# Action API skeleton
# ---------------------------------------------------------------------------


class TestActionSkeleton:
    def test_not_implemented_receipt_shape(self):
        svc = AuraBrowserService()
        r = svc.click_selector("#btn")
        assert r.operation == "click_selector"
        assert r.action_status == "not_implemented"
        assert r.ok is False
        assert "click_selector" in r.phase_errors
        assert r.metadata["selector"] == "#btn"

    def test_click_text_not_implemented(self):
        svc = AuraBrowserService()
        r = svc.click_text("Submit")
        assert r.action_status == "not_implemented"
        assert r.metadata["search_text"] == "Submit"

    def test_type_text_not_implemented(self):
        svc = AuraBrowserService()
        r = svc.type_text("hello", selector="#input")
        assert r.action_status == "not_implemented"
        assert r.metadata["text"] == "hello"
        assert r.metadata["selector"] == "#input"

    def test_press_key_not_implemented(self):
        svc = AuraBrowserService()
        r = svc.press_key("Enter")
        assert r.action_status == "not_implemented"
        assert r.metadata["key"] == "Enter"

    def test_scroll_not_implemented(self):
        svc = AuraBrowserService()
        r = svc.scroll(delta_y=100, direction="down")
        assert r.action_status == "not_implemented"
        assert r.metadata["delta_y"] == 100
        assert r.metadata["direction"] == "down"

    def test_select_option_not_implemented(self):
        svc = AuraBrowserService()
        r = svc.select_option("#country", "US")
        assert r.action_status == "not_implemented"
        assert r.metadata["selector"] == "#country"
        assert r.metadata["value"] == "US"


# ---------------------------------------------------------------------------
# Receipt model
# ---------------------------------------------------------------------------


class TestReceiptModel:
    def test_new_receipt_fields_have_defaults(self):
        r = BrowserReceipt()
        assert r.operation == ""
        assert r.observation_status == "not_started"
        assert r.action_status == "not_started"
        assert r.session_id == ""
        assert r.reused_existing is False
        assert r.page_index == -1
        assert r.page_count == 0
        assert r.metadata == {}

    def test_ok_false_for_not_implemented_action(self):
        r = BrowserReceipt(
            browser_ready=True,
            operation="click_selector",
            action_status="not_implemented",
        )
        assert r.ok is False

    def test_ok_false_for_failed_observation(self):
        r = BrowserReceipt(
            browser_ready=True,
            observation_status="failed",
        )
        assert r.ok is False

    def test_to_dict_includes_new_fields(self):
        r = BrowserReceipt(
            operation="search",
            observation_status="success",
            action_status="not_started",
            session_id="sess-1",
            reused_existing=True,
            page_index=0,
            page_count=3,
            metadata={"key": "value"},
        )
        d = r.to_dict()
        assert d["operation"] == "search"
        assert d["observation_status"] == "success"
        assert d["session_id"] == "sess-1"
        assert d["reused_existing"] is True
        assert d["metadata"] == {"key": "value"}


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------


class TestSessionModel:
    def test_session_defaults(self):
        s = BrowserSession()
        assert s.session_id != ""
        assert s.started_at != ""
        assert s.connected is False
        assert s.closed is False
        assert s.page_count == 0
        assert s.active_page_index == -1
        assert s.reused_existing is False

    def test_session_to_dict_round_trip(self):
        s = BrowserSession(
            session_id="s1",
            browser_executable="/usr/bin/chrome",
            connected=True,
            page_count=2,
        )
        d = s.to_dict()
        assert d["session_id"] == "s1"
        assert d["connected"] is True
        assert d["page_count"] == 2


# ---------------------------------------------------------------------------
# PageInfo model
# ---------------------------------------------------------------------------


class TestPageInfoModel:
    def test_page_info_to_dict(self):
        pi = PageInfo(index=0, url="https://example.com", title="Example", is_active=True)
        d = pi.to_dict()
        assert d["index"] == 0
        assert d["url"] == "https://example.com"
        assert d["is_active"] is True
