"""Offscreen GUI regressions for the real chat composer entry points."""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from aura.gui.composer_skills import ComposerSkill, ComposerSkillsWidget
from aura.gui.input_panel import Attachment, InputPanel, SendPayload
from aura.gui.send_handler import QueuedItem, SendHandler


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_on_submit_emits_while_idle_and_execution_active(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path)
    payloads = []
    panel.sent.connect(payloads.append)

    panel.set_text("idle")
    panel._on_submit()
    panel.set_execution_active(True)
    panel.set_text("queued")
    panel._on_submit()

    assert [payload.text for payload in payloads] == ["idle", "queued"]
    assert panel._editor.toPlainText() == ""


def test_input_panel_no_longer_owns_handoff_action(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path)

    assert not hasattr(panel, "_handoff_btn")
    assert not hasattr(panel, "handoff_requested")


def test_send_button_emits_sent(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path)
    payloads = []
    panel.sent.connect(payloads.append)
    panel.set_text("button")

    QTest.mouseClick(panel._send_btn, Qt.MouseButton.LeftButton)

    assert [payload.text for payload in payloads] == ["button"]


def test_ctrl_enter_emits_sent(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path)
    payloads = []
    panel.sent.connect(payloads.append)
    panel.set_text("keyboard")

    QTest.keyClick(
        panel._editor,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ControlModifier,
    )

    assert [payload.text for payload in payloads] == ["keyboard"]


def test_active_send_button_and_ctrl_enter_both_emit(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path)
    payloads = []
    panel.sent.connect(payloads.append)
    panel.set_execution_active(True)

    panel.set_text("button follow-up")
    QTest.mouseClick(panel._send_btn, Qt.MouseButton.LeftButton)
    panel.set_text("keyboard follow-up")
    QTest.keyClick(
        panel._editor,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ControlModifier,
    )

    assert [payload.text for payload in payloads] == [
        "button follow-up",
        "keyboard follow-up",
    ]


def test_composer_skill_chips_add_dedupe_order_remove_and_clear() -> None:
    _app()
    skills = ComposerSkillsWidget()

    assert skills.select_installed_skill("project:first", "First") is True
    assert skills.select_installed_skill("project:first", "Duplicate") is False
    assert skills.select_installed_skill("personal:second", "Second") is True
    assert skills.selection == (
        ComposerSkill("project:first", "First"),
        ComposerSkill("personal:second", "Second"),
    )
    assert skills.isVisible() is True

    assert skills.remove_installed_skill("project:first") is True
    assert skills.selection == (ComposerSkill("personal:second", "Second"),)
    skills.clear()
    assert skills.selection == ()
    assert skills.isVisible() is False


def test_send_payload_freezes_and_clears_selected_skills(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path)
    payloads = []
    panel.sent.connect(payloads.append)
    panel.select_installed_skill("project:first", "First")
    panel.select_installed_skill("personal:second", "Second")
    panel.set_text("Use both")

    panel._on_submit()
    panel.select_installed_skill("bundled:later", "Later")

    assert payloads[0].selected_skills == (
        ComposerSkill("project:first", "First"),
        ComposerSkill("personal:second", "Second"),
    )
    assert panel.selected_skills() == (ComposerSkill("bundled:later", "Later"),)


def test_skill_only_is_not_sendable_and_rejected_payload_restores_chips(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path)
    payloads = []
    panel.sent.connect(payloads.append)
    panel.select_installed_skill("project:test", "Test")

    panel._on_submit()

    assert payloads == []
    assert panel._send_btn.isEnabled() is False

    payload = SendPayload(
        "Look",
        [],
        (ComposerSkill("project:restored", "Restored"),),
    )
    panel.restore_payload(payload)
    assert panel._editor.toPlainText() == "Look"
    assert panel.selected_skills() == payload.selected_skills


def test_workspace_change_clears_unsent_skill_chips(tmp_path) -> None:
    _app()
    panel = InputPanel(tmp_path / "first")
    panel.select_installed_skill("project:test", "Test")

    panel.set_workspace_root(tmp_path / "second")

    assert panel.selected_skills() == ()


def test_stop_clears_submitted_queue_but_preserves_draft_and_attachments(
    tmp_path,
) -> None:
    _app()
    panel = InputPanel(tmp_path)

    class Bridge:
        def __init__(self) -> None:
            self.cancelled = False

        def request_cancel(self) -> None:
            self.cancelled = True

    bridge = Bridge()
    handler = SendHandler(
        bridge=bridge,
        chat=SimpleNamespace(),
        input_panel=panel,
        settings=SimpleNamespace(),
        workspace_root=tmp_path,
    )
    handler._message_queue.extend(
        [
            QueuedItem("one", [], "model", "off"),
            QueuedItem("two", [], "model", "high"),
        ]
    )
    panel.set_queued_messages(2)
    attachment = Attachment(
        kind="file",
        name="draft.txt",
        b64=None,
        text_ref="[user attached: draft.txt]",
    )
    panel.set_text("unsent draft")
    panel.set_attachments([attachment])

    handler.handle_stop()

    assert bridge.cancelled is True
    assert handler._message_queue == []
    assert panel._queued_count == 0
    assert panel._editor.toPlainText() == "unsent draft"
    assert panel._attachments == [attachment]
