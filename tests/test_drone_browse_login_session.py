from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from aura.drones.browse.artifacts import build_login_session_artifact
from aura.drones.browse.login_session import run_login_session


def test_build_login_session_artifact_merges_metadata() -> None:
    artifact = build_login_session_artifact(
        start_url="https://example.com",
        final_url="https://example.com/home",
        page_title="Home",
        status="login_session_closed",
        browser_profile="my-profile",
        action_trace=[],
        errors=[],
        profile_metadata={"visible": False, "extra_meta": 42},
        browser_metadata={"browser_id": "b123", "browser_label": "Chrome"},
    )
    assert artifact["browser_profile"] == "my-profile"
    assert artifact["status"] == "login_session_closed"
    assert artifact["visible"] is False
    assert artifact["extra_meta"] == 42
    assert artifact["browser_id"] == "b123"
    assert artifact["browser_label"] == "Chrome"


def test_run_login_session_success_includes_browser_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockPage:
        def __init__(self):
            self.url = "https://example.com/dashboard"
        def goto(self, url, **kwargs):
            pass
        def title(self):
            return "Dashboard"
            
    class MockContext:
        def __init__(self):
            self.pages = [MockPage()]
        def new_page(self):
            return self.pages[0]

    class MockRuntime:
        def __init__(self, **kwargs):
            self.context = MockContext()
            self.route_metadata = {
                "browser_id": "test-b-1",
                "browser_label": "TestBrowser",
                "browser_source": "system",
                "browser_persistent": True,
                "browser_visible": True,
                "attempted_routes": ["route1", "route2"]
            }
            self.unavailable_reason = None
            
        def start(self):
            return True
            
        def close(self):
            pass

    monkeypatch.setattr("aura.drones.browse.login_session.BrowserRuntime", MockRuntime)
    monkeypatch.setattr("aura.drones.browse.login_session.ensure_profile_dir", lambda p: p)

    on_content_calls = []
    
    cancel_event = MagicMock()
    cancel_event.is_set.return_value = True

    result = run_login_session(
        start_url="https://example.com",
        browser_profile="my-profile",
        wait_seconds=10,
        on_content=on_content_calls.append,
        cancel_event=cancel_event
    )

    assert result["status"] == "login_session_cancelled"
    assert result["browser_id"] == "test-b-1"
    assert result["browser_label"] == "TestBrowser"
    assert result["browser_source"] == "system"
    assert result["browser_persistent"] is True
    assert result["browser_visible"] is True
    assert result["attempted_routes"] == ["route1", "route2"]


def test_run_login_session_failed_start_includes_attempted_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockRuntimeFailing:
        def __init__(self, **kwargs):
            self.route_metadata = {
                "attempted_routes": ["route-a"]
            }
            self.unavailable_reason = "No browser found"
            
        def start(self):
            return False
            
        def close(self):
            pass

    monkeypatch.setattr("aura.drones.browse.login_session.BrowserRuntime", MockRuntimeFailing)
    monkeypatch.setattr("aura.drones.browse.login_session.ensure_profile_dir", lambda p: p)

    result = run_login_session(
        start_url="https://example.com",
        browser_profile="my-profile",
        wait_seconds=10,
        on_content=lambda x: None,
    )

    assert result["status"] == "login_session_failed"
    assert "No browser found" in result["errors"][0]
    assert result["attempted_routes"] == ["route-a"]


def test_run_login_session_page_failure_does_not_mask_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockContext:
        pages = []

        def new_page(self):
            raise RuntimeError("new page failed")

    class MockRuntime:
        def __init__(self, **kwargs):
            self.context = MockContext()
            self.route_metadata = {
                "browser_id": "test-b-2",
                "browser_label": "TestBrowser",
                "browser_source": "system",
                "browser_persistent": True,
                "browser_visible": True,
                "attempted_routes": ["route1"],
            }

        def start(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr("aura.drones.browse.login_session.BrowserRuntime", MockRuntime)
    monkeypatch.setattr("aura.drones.browse.login_session.ensure_profile_dir", lambda p: p)

    result = run_login_session(
        start_url="https://example.com",
        browser_profile="my-profile",
        wait_seconds=10,
        on_content=lambda x: None,
    )

    assert result["status"] == "login_session_failed"
    assert result["errors"] == ["new page failed"]
    assert result["browser_id"] == "test-b-2"
    assert result["attempted_routes"] == ["route1"]
