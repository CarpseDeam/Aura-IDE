"""Regression checks for the toolbar utility grouping and the edge rail's
move from a floating overlay to a real layout-managed column."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from aura.settings import AppSettings  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_toolbar_has_no_expanding_spacer_between_logs_and_settings(qapp) -> None:
    from aura.gui.main_window_toolbar import MainWindowToolbar

    toolbar = MainWindowToolbar(AppSettings())
    actions = toolbar.actions()
    logs_idx = next(
        i for i, a in enumerate(actions) if toolbar.widgetForAction(a) is toolbar._logs_btn
    )
    settings_idx = next(i for i, a in enumerate(actions) if a.text() == "Settings")
    assert settings_idx > logs_idx

    for action in actions[logs_idx + 1 : settings_idx]:
        widget = toolbar.widgetForAction(action)
        if widget is not None:
            assert widget.sizePolicy().horizontalPolicy() != QSizePolicy.Policy.Expanding


def test_edge_tab_rail_is_fixed_width_and_vertically_expanding(qapp) -> None:
    from aura.gui.edge_rails import EdgeTabRail

    rail = EdgeTabRail()
    assert rail.minimumWidth() == 40
    assert rail.maximumWidth() == 40

    policy = rail.sizePolicy()
    assert policy.horizontalPolicy() == QSizePolicy.Policy.Fixed
    assert policy.verticalPolicy() == QSizePolicy.Policy.Expanding


def test_main_window_no_longer_owns_manual_rail_geometry() -> None:
    from aura.gui.main_window import MainWindow

    assert not hasattr(MainWindow, "_position_edge_tabs")
    assert "resizeEvent" not in MainWindow.__dict__
