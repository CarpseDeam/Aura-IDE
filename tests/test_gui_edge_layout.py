"""Regression checks for the toolbar utility grouping and the edge rail's
external, protruding-tab presentation (not an internal layout gutter)."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QSizePolicy  # noqa: E402

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


def test_agents_is_the_third_glass_switch_in_the_historical_toolbar_slot(qapp) -> None:
    from aura.gui.main_window_toolbar import MainWindowToolbar
    from aura.gui.theme import LABEL_AGENTS
    from aura.gui.widgets.glass_switch import GlassSwitch

    toolbar = MainWindowToolbar(AppSettings())
    switches = [
        toolbar.widgetForAction(action)
        for action in toolbar.actions()
        if isinstance(toolbar.widgetForAction(action), GlassSwitch)
    ]

    assert switches == [
        toolbar._plan_review_switch,
        toolbar._auto_approve_switch,
        toolbar._agents_switch,
    ]
    assert [switch._label_text for switch in switches] == ["Plan", "Approve", "Agents"]
    assert toolbar._agents_switch._accent_color == LABEL_AGENTS
    assert toolbar._agents_switch.isChecked() is False
    assert toolbar._agents_switch.isEnabled() is False


def test_agents_switch_is_controller_driven_and_emits_user_intent(qapp) -> None:
    from aura.gui.main_window_toolbar import MainWindowToolbar

    toolbar = MainWindowToolbar(AppSettings())
    toggled: list[bool] = []
    toolbar.agents_toggled.connect(toggled.append)

    toolbar.set_agents_available(True)
    toolbar.set_agents_enabled(True)

    assert toolbar.agents_enabled() is True
    assert toolbar._agents_switch.isEnabled() is True
    assert toggled == []

    toolbar._agents_switch.toggled.emit(False)
    assert toggled == [False]


def test_initial_agents_gate_refresh_happens_after_signal_wiring() -> None:
    import inspect

    from aura.gui.main_window import MainWindow

    source = inspect.getsource(MainWindow.__init__)
    assert source.index("self._signal_wiring.wire()") < source.index(
        "self._agents_controller.refresh_workflow_gate()"
    )


def test_edge_tab_rail_is_fixed_width_and_compact(qapp) -> None:
    """The rail must size itself to its own content (terminal/checkpoint/
    agents/companion tabs), not stretch to fill a parent column."""
    from aura.gui.edge_rails import EdgeTabRail

    rail = EdgeTabRail()
    assert rail.minimumWidth() == 40
    assert rail.maximumWidth() == 40

    policy = rail.sizePolicy()
    assert policy.verticalPolicy() != QSizePolicy.Policy.Expanding


def test_edge_rail_host_is_frameless_tool_window_owning_the_rail(qapp) -> None:
    from aura.gui.edge_rail_host import ExternalEdgeRailHost

    main = QMainWindow()
    try:
        host = ExternalEdgeRailHost(main)

        assert bool(host.windowFlags() & Qt.WindowType.Tool)
        assert bool(host.windowFlags() & Qt.WindowType.FramelessWindowHint)
        assert host.parent() is main

        from aura.gui.edge_rails import EdgeTabRail
        assert isinstance(host.rail, EdgeTabRail)
    finally:
        main.deleteLater()


def test_main_window_central_widget_is_just_the_splitter_no_rail_gutter() -> None:
    """The main splitter must be the central widget directly — no wrapping
    container that carves a fixed-width rail column out of it."""
    import ast
    import inspect

    from aura.gui import main_window

    source = inspect.getsource(main_window)
    tree = ast.parse(source)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "setCentralWidget"
    ]
    assert len(calls) == 1
    (arg,) = calls[0].args
    assert isinstance(arg, ast.Attribute) and arg.attr == "_main_splitter"


def test_old_scattered_position_edge_tabs_pattern_does_not_return() -> None:
    import inspect

    from aura.gui import main_window, main_window_agents, main_window_terminal

    for module in (main_window, main_window_agents, main_window_terminal):
        assert "_position_edge_tabs" not in inspect.getsource(module)
