from __future__ import annotations

from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from aura.gui.main_window_toolbar import MainWindowToolbar


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_new_conversation_action_emits_no_arg_signal(qapp) -> None:
    settings = Mock()
    settings.auto_dispatch = False
    settings.auto_approve = False
    toolbar = MainWindowToolbar(settings)
    received: list[bool] = []
    toolbar.new_conversation_requested.connect(lambda: received.append(True))

    action = next(a for a in toolbar.actions() if a.text() == "New Conversation")
    action.trigger()

    assert received == [True]


def test_open_conversation_action_emits_no_arg_signal(qapp) -> None:
    settings = Mock()
    settings.auto_dispatch = False
    settings.auto_approve = False
    toolbar = MainWindowToolbar(settings)
    received: list[bool] = []
    toolbar.open_conversation_requested.connect(lambda: received.append(True))

    action = next(a for a in toolbar.actions() if a.text() == "Open Conversation...")
    action.trigger()

    assert received == [True]
