"""Tests for InfoHubPane."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from aura.gui.info_hub_pane import InfoHubPane


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_worker_log_appends_incrementally(qapp) -> None:
    pane = InfoHubPane()
    pane.append_content("Hello")
    assert pane._log_buffer == "Hello"
    assert pane._log_visible == ""

    # Tick enough times to reveal text incrementally
    pane._on_log_tick()
    assert pane._log_visible == "Hello"
    assert pane._log_view.toPlainText() == "Hello"

    # Append more text
    pane.append_content(" World")
    assert pane._log_buffer == "Hello World"
    
    pane._on_log_tick()
    assert pane._log_visible == "Hello World"
    assert pane._log_view.toPlainText() == "Hello World"


def test_worker_log_flush(qapp) -> None:
    pane = InfoHubPane()
    pane.append_content("A very long string that should not be fully revealed in one tick")
    assert pane._log_visible == ""
    pane._flush_log()
    assert pane._log_visible == "A very long string that should not be fully revealed in one tick"
    assert pane._log_view.toPlainText() == "A very long string that should not be fully revealed in one tick"
