"""Plan Review setting: defaults, round-trip, and toolbar/persistence wiring."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.settings import AppSettings  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_review_plan_before_changes_defaults_off() -> None:
    assert AppSettings().review_plan_before_changes is False


def test_review_plan_before_changes_round_trips_true() -> None:
    settings = AppSettings.from_dict({"review_plan_before_changes": True})
    assert settings.review_plan_before_changes is True


def test_review_plan_before_changes_ignores_invalid_type() -> None:
    settings = AppSettings.from_dict({"review_plan_before_changes": "yes"})
    assert settings.review_plan_before_changes is False


def test_plan_toggle_widget_defaults_from_settings_and_re_emits(qapp) -> None:
    from aura.gui.main_window_toolbar import MainWindowToolbar

    on_settings = AppSettings(review_plan_before_changes=True)
    toolbar_on = MainWindowToolbar(on_settings)
    assert toolbar_on._plan_review_switch.isChecked() is True

    settings = AppSettings()
    toolbar = MainWindowToolbar(settings)
    assert toolbar._plan_review_switch.isChecked() is False

    received: list[bool] = []
    toolbar.plan_review_toggled.connect(received.append)
    # Drive the switch's own toggled signal, as a real click would, to prove
    # the toolbar re-emits it as plan_review_toggled.
    toolbar._plan_review_switch.toggled.emit(True)
    assert received == [True]

    toolbar.set_plan_review(True)
    assert toolbar._plan_review_switch.isChecked() is True
    toolbar.set_plan_review(False)
    assert toolbar._plan_review_switch.isChecked() is False


def test_plan_toggle_tooltip_mentions_next_request(qapp) -> None:
    from aura.gui.main_window_toolbar import MainWindowToolbar

    toolbar = MainWindowToolbar(AppSettings())
    tooltip = toolbar._plan_review_switch.toolTip()
    assert "next request" in tooltip.lower()


def test_settings_controller_persists_plan_toggle(qapp, monkeypatch) -> None:
    from aura.gui.main_window_settings import MainWindowSettingsController

    saved: dict = {}
    monkeypatch.setattr(
        "aura.gui.main_window_settings.save_settings",
        lambda s: saved.__setitem__("settings", s),
    )

    class FakeBridge:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def set_review_plan_before_changes(self, value: bool) -> None:
            self.calls.append(value)

    class FakeToolbar:
        def refresh_auto_toggle_tooltips(self) -> None:
            pass

    class FakeWindow:
        pass

    window = FakeWindow()
    window._settings = AppSettings()
    window._bridge = FakeBridge()
    window._toolbar = FakeToolbar()

    controller = MainWindowSettingsController(window)
    controller.on_plan_review_toggled(True)

    assert window._settings.review_plan_before_changes is True
    assert window._bridge.calls == [True]
    assert saved["settings"].review_plan_before_changes is True
