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


def test_worker_log_reveals_in_chunks(qapp) -> None:
    pane = InfoHubPane()
    chunk = pane._LOG_REVEAL_CHARS_PER_TICK
    # Feed text longer than one reveal chunk
    long_text = "Hello World, this is a very long text that should span multiple ticks."
    assert len(long_text) > chunk
    pane.append_content(long_text)

    # After first tick, only first chunk chars should be visible
    pane._on_log_tick()
    expected_first = long_text[:chunk]
    assert pane._log_visible == expected_first, f"Expected '{expected_first}', got '{pane._log_visible}'"
    assert pane._log_view.toPlainText() == expected_first

    # After second tick, next chunk appended, previous text preserved
    pane._on_log_tick()
    expected_second = long_text[: chunk * 2]
    assert pane._log_visible == expected_second, f"Expected '{expected_second}', got '{pane._log_visible}'"
    assert pane._log_view.toPlainText() == expected_second

    # After enough ticks, full text is revealed
    while pane._log_visible != long_text:
        pane._on_log_tick()
    assert pane._log_visible == long_text
    assert pane._log_view.toPlainText() == long_text
