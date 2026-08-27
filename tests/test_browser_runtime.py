"""Focused coverage for the installed-browser Playwright runtime."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aura.browser import runtime


def _choice(
    browser_id: str,
    *,
    channel: str | None = None,
    executable_path: str | None = None,
) -> runtime.BrowserChoice:
    labels = {"chrome": "Google Chrome", "edge": "Microsoft Edge", "brave": "Brave"}
    return runtime.BrowserChoice(browser_id, labels[browser_id], channel, executable_path)


def _install_playwright_mock(monkeypatch: pytest.MonkeyPatch, playwrights: list[MagicMock]) -> MagicMock:
    starters = iter(playwrights)
    sync_playwright = MagicMock(side_effect=lambda: SimpleNamespace(start=lambda: next(starters)))
    playwright_module = ModuleType("playwright")
    sync_api_module = ModuleType("playwright.sync_api")
    sync_api_module.sync_playwright = sync_playwright
    playwright_module.sync_api = sync_api_module
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)
    return sync_playwright


def _playwright(*, browser: MagicMock | None = None, context: MagicMock | None = None) -> MagicMock:
    pw = MagicMock()
    pw.chromium.launch.return_value = browser or MagicMock()
    pw.chromium.launch_persistent_context.return_value = context or MagicMock()
    return pw


def test_detects_only_installed_browsers_in_priority_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)

    choices = runtime._detect_installed_browsers()

    assert [choice.id for choice in choices] == ["chrome", "edge", "brave"]
    assert [choice.channel for choice in choices] == ["chrome", "msedge", None]
    assert choices[2].executable_path.endswith("BraveSoftware\\Brave-Browser\\Application\\brave.exe")


def test_detection_returns_no_managed_chromium_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)

    assert runtime._detect_installed_browsers() == []


def test_no_supported_browser_fails_without_starting_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_detect_installed_browsers", lambda: [])
    sync_playwright = _install_playwright_mock(monkeypatch, [])

    browser_runtime = runtime.BrowserRuntime()

    assert browser_runtime.start() is False
    assert sync_playwright.call_count == 0
    assert browser_runtime.route_metadata["attempted_routes"] == []
    assert "installed Google Chrome, Microsoft Edge, or Brave" in browser_runtime.unavailable_reason


def test_falls_through_installed_routes_and_reports_truthful_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = [_choice("chrome", channel="chrome"), _choice("edge", channel="msedge")]
    monkeypatch.setattr(runtime, "_detect_installed_browsers", lambda: choices)
    failed = _playwright()
    failed.chromium.launch.side_effect = RuntimeError("Chrome route failed")
    browser = MagicMock()
    context = MagicMock()
    browser.new_context.return_value = context
    succeeded = _playwright(browser=browser)
    _install_playwright_mock(monkeypatch, [failed, succeeded])

    browser_runtime = runtime.BrowserRuntime(headless=True)

    assert browser_runtime.start() is True
    failed.stop.assert_called_once_with()
    succeeded.chromium.launch.assert_called_once_with(headless=True, channel="msedge")
    assert browser_runtime.context is context
    assert browser_runtime.route_metadata == {
        "browser_id": "edge",
        "browser_label": "Microsoft Edge",
        "browser_source": "installed",
        "browser_persistent": False,
        "browser_visible": False,
        "attempted_routes": ["chrome"],
    }


@pytest.mark.parametrize(
    "choice, expected",
    [
        (_choice("chrome", channel="chrome"), {"headless": False, "channel": "chrome"}),
        (_choice("edge", channel="msedge"), {"headless": False, "channel": "msedge"}),
        (
            _choice("brave", executable_path="C:\\Brave\\brave.exe"),
            {"headless": False, "executable_path": "C:\\Brave\\brave.exe"},
        ),
    ],
)
def test_anonymous_launch_arguments(
    monkeypatch: pytest.MonkeyPatch,
    choice: runtime.BrowserChoice,
    expected: dict[str, object],
) -> None:
    monkeypatch.setattr(runtime, "_detect_installed_browsers", lambda: [choice])
    browser = MagicMock()
    pw = _playwright(browser=browser)
    _install_playwright_mock(monkeypatch, [pw])

    browser_runtime = runtime.BrowserRuntime(headless=False)

    assert browser_runtime.start() is True
    pw.chromium.launch.assert_called_once_with(**expected)
    browser.new_context.assert_called_once_with()


@pytest.mark.parametrize(
    "choice, route_argument",
    [
        (_choice("chrome", channel="chrome"), ("channel", "chrome")),
        (_choice("edge", channel="msedge"), ("channel", "msedge")),
        (_choice("brave", executable_path="C:\\Brave\\brave.exe"), ("executable_path", "C:\\Brave\\brave.exe")),
    ],
)
def test_persistent_profile_launch_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    choice: runtime.BrowserChoice,
    route_argument: tuple[str, str],
) -> None:
    profile = tmp_path / "browser-profile"
    monkeypatch.setattr(runtime, "_detect_installed_browsers", lambda: [choice])
    context = MagicMock()
    pw = _playwright(context=context)
    _install_playwright_mock(monkeypatch, [pw])

    browser_runtime = runtime.BrowserRuntime(headless=True, user_data_dir=profile)

    assert browser_runtime.start() is True
    expected = {"user_data_dir": str(profile), "headless": True, route_argument[0]: route_argument[1]}
    pw.chromium.launch_persistent_context.assert_called_once_with(**expected)
    assert profile.is_dir()
    assert browser_runtime.route_metadata["browser_persistent"] is True


def test_frozen_runtime_does_not_mutate_playwright_browser_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "machine-owned")
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime, "_detect_installed_browsers", lambda: [])

    assert runtime.BrowserRuntime().start() is False
    assert runtime.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "machine-owned"


def test_total_launch_failure_keeps_route_and_final_error_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = [_choice("chrome", channel="chrome"), _choice("edge", channel="msedge")]
    monkeypatch.setattr(runtime, "_detect_installed_browsers", lambda: choices)
    chrome = _playwright()
    chrome.chromium.launch.side_effect = RuntimeError("chrome unavailable")
    edge = _playwright()
    edge.chromium.launch.side_effect = RuntimeError("edge unavailable")
    _install_playwright_mock(monkeypatch, [chrome, edge])

    browser_runtime = runtime.BrowserRuntime()

    assert browser_runtime.start() is False
    assert browser_runtime.route_metadata["attempted_routes"] == ["chrome", "edge"]
    assert "installed Google Chrome, Microsoft Edge, or Brave" in browser_runtime.unavailable_reason
    assert "chrome, edge" in browser_runtime.unavailable_reason
    assert "Final error: edge unavailable" in browser_runtime.unavailable_reason
    chrome.stop.assert_called_once_with()
    edge.stop.assert_called_once_with()


def test_partial_launch_failure_is_cleaned_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime,
        "_detect_installed_browsers",
        lambda: [_choice("chrome", channel="chrome")],
    )
    browser = MagicMock()
    browser.new_context.side_effect = RuntimeError("context creation failed")
    pw = _playwright(browser=browser)
    _install_playwright_mock(monkeypatch, [pw])

    browser_runtime = runtime.BrowserRuntime()

    assert browser_runtime.start() is False
    browser.close.assert_called_once_with()
    pw.stop.assert_called_once_with()
    assert browser_runtime.context is None


def test_close_continues_after_individual_cleanup_failures() -> None:
    browser_runtime = runtime.BrowserRuntime()
    context = MagicMock()
    context.close.side_effect = RuntimeError("already gone")
    browser = MagicMock()
    playwright = MagicMock()
    browser_runtime._context = context
    browser_runtime._browser = browser
    browser_runtime._pw = playwright

    browser_runtime.close()
    browser_runtime.close()

    context.close.assert_called_once_with()
    browser.close.assert_called_once_with()
    playwright.stop.assert_called_once_with()
    assert browser_runtime.context is None
