"""Tests for the Aura updater dialog."""

from __future__ import annotations

import pytest

from aura.updater import PullResult

update_dialog = pytest.importorskip("aura.gui.update_dialog")


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text


class _FakePlainTextEdit:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def toPlainText(self) -> str:
        return "\n".join(self.lines)

    def appendPlainText(self, text: str) -> None:
        self.lines.append(text)


def test_packaged_update_failure_does_not_request_app_quit(monkeypatch) -> None:
    monkeypatch.setattr(update_dialog, "is_packaged", lambda: True)
    dialog = update_dialog.UpdateDialog.__new__(update_dialog.UpdateDialog)
    dialog._summary = _FakeLabel()
    dialog._output = _FakePlainTextEdit()

    installer_path = r"C:\Users\tester\AppData\Local\Aura\updates\1.4.6\AuraSetup-1.4.6.exe"
    result = PullResult(
        False,
        None,
        message=(
            "Failed to launch the installer.\n"
            f"Downloaded installer: {installer_path}\n"
            "Launch method: ShellExecuteW/open\n"
            f"If the installer does not appear, run this file manually: {installer_path}"
        ),
    )

    dialog._show_pull_result(result)

    output = dialog._output.toPlainText()
    assert dialog._summary.text == result.message
    assert installer_path in output
    assert "Aura is still running" in output
