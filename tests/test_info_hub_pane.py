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


def test_worker_log_batches_tiny_fragments_until_flush(qapp) -> None:
    pane = InfoHubPane()

    pane.append_content("Hel")
    pane.append_content("lo")
    pane.append_content(" World")

    assert pane._log_view.toPlainText() == ""

    pane._log_stream.flush()

    assert pane._log_view.toPlainText() == "Hello World"


def test_worker_log_separates_reasoning_and_content(qapp) -> None:
    pane = InfoHubPane()

    pane.append_reasoning("Checking files")
    pane.append_content("Now applying changes")
    pane._log_stream.flush()

    assert pane._log_view.toPlainText() == "Checking files\n\nNow applying changes"


def test_final_summary_flushes_pending_prose_first(qapp) -> None:
    pane = InfoHubPane()

    pane.append_content("Pending prose")
    pane.show_final_summary(True, "Done")

    text = pane._log_view.toPlainText()
    assert text.startswith("Pending prose\n\n")
    assert "Worker completed successfully." in text
    assert text.index("Pending prose") < text.index("Worker completed successfully.")


def test_clear_drops_pending_prose(qapp) -> None:
    pane = InfoHubPane()

    pane.append_content("stale")
    pane.clear()
    pane._log_stream.flush()

    assert pane._log_view.toPlainText() == ""
