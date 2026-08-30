"""Focused GUI lifecycle checks for collaborative versus production turns."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.gui.main_window import MainWindow  # noqa: E402
from aura.gui.status_bar import AuraStatusBar  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


def _window_for_started(active_turn_read_only: bool, status_bar: AuraStatusBar):
    switch_to_workspace = _Recorder()
    return SimpleNamespace(
        _final_stream_message={},
        _input=SimpleNamespace(
            set_execution_active=_Recorder(),
            focus_editor=_Recorder(),
        ),
        # Skill lifecycle actions are shut off for the length of a turn.
        _skills_controller=SimpleNamespace(set_execution_active=_Recorder()),
        # So are agent definitions, the roster, and permission grants.
        _agents_controller=SimpleNamespace(set_execution_active=_Recorder()),
        _status_bar=status_bar,
        _bridge=SimpleNamespace(active_turn_read_only=active_turn_read_only),
        _playground=SimpleNamespace(switch_to_workspace=switch_to_workspace),
        _chat=SimpleNamespace(
            assistant_done=_Recorder(),
            stop_current_aura=_Recorder(),
        ),
        _settle_finished_turn=_Recorder(),
    ), switch_to_workspace


@pytest.mark.parametrize("active_turn_read_only", [True, False])
def test_started_keeps_collaborative_presentation_but_switches_production(
    qapp, active_turn_read_only
) -> None:
    bar = AuraStatusBar()
    try:
        window, switch_to_workspace = _window_for_started(
            active_turn_read_only, bar
        )
        MainWindow._on_started(window)

        assert not bar._handoff_btn.isEnabled()
        if active_turn_read_only:
            assert switch_to_workspace.calls == []
        else:
            assert switch_to_workspace.calls == [()]
    finally:
        bar.deleteLater()


def test_handoff_tracks_bridge_start_and_finish_for_both_turn_modes(
    qapp, monkeypatch
) -> None:
    """The footer follows MainWindow's bridge lifecycle, not production-only events."""
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda *_args: None))
    bar = AuraStatusBar()
    try:
        for active_turn_read_only in (True, False):
            window, _switch = _window_for_started(active_turn_read_only, bar)

            MainWindow._on_started(window)
            assert not bar._handoff_btn.isEnabled()

            MainWindow._on_finished(window)
            assert bar._handoff_btn.isEnabled()
            # Skill mutations are refused while the turn runs, then restored.
            assert window._skills_controller.set_execution_active.calls == [
                (True,),
                (False,),
            ]
            assert window._agents_controller.set_execution_active.calls == [
                (True,),
                (False,),
            ]
    finally:
        bar.deleteLater()
