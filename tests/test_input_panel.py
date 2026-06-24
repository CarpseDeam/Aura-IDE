from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from aura.gui.input_panel import Attachment, InputPanel, SendPayload


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_input_panel_starts_with_disabled_send_button(qapp: QApplication, tmp_path) -> None:
    panel = InputPanel(tmp_path)

    assert panel._attachments == []
    assert not panel._send_btn.isEnabled()
    assert panel._editor.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert panel._editor.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert panel._send_btn.minimumWidth() <= 40
    assert panel._send_btn.minimumHeight() <= 30

    panel.set_text("hello")

    assert panel._send_btn.isEnabled()


def test_send_button_disables_after_attachment_only_submit(qapp: QApplication, tmp_path) -> None:
    panel = InputPanel(tmp_path)
    attachment = Attachment(
        kind="file",
        name="README.md",
        b64=None,
        text_ref="[user attached: README.md]",
    )
    emitted = []
    panel.sent.connect(emitted.append)

    panel.set_attachments([attachment])
    assert panel._send_btn.isEnabled()

    panel._on_submit()

    assert emitted == [SendPayload(text="", attachments=[attachment])]
    assert panel._attachments == []
    assert not panel._send_btn.isEnabled()
