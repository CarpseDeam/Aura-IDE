"""Tests for the Aura updater dialog."""

from __future__ import annotations

from pathlib import Path

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


class _FakeMessageBox:
    last: "_FakeMessageBox | None" = None
    next_clicked_label = "Open Installer"

    class Icon:
        Information = object()

    class ButtonRole:
        AcceptRole = object()
        RejectRole = object()

    def __init__(self, parent) -> None:
        self.parent = parent
        self.title = ""
        self.icon = None
        self.text = ""
        self.informative_text = ""
        self.buttons: dict[str, object] = {}
        self.default_button = None
        self._clicked_button = None
        _FakeMessageBox.last = self

    def setWindowTitle(self, title: str) -> None:
        self.title = title

    def setIcon(self, icon) -> None:
        self.icon = icon

    def setText(self, text: str) -> None:
        self.text = text

    def setInformativeText(self, text: str) -> None:
        self.informative_text = text

    def addButton(self, label: str, role) -> object:
        button = object()
        self.buttons[label] = button
        return button

    def setDefaultButton(self, button) -> None:
        self.default_button = button

    def exec(self) -> None:
        self._clicked_button = self.buttons[self.next_clicked_label]

    def clickedButton(self):
        return self._clicked_button


def _fake_dialog():
    dialog = update_dialog.UpdateDialog.__new__(update_dialog.UpdateDialog)
    dialog._summary = _FakeLabel()
    dialog._output = _FakePlainTextEdit()
    return dialog


def test_installer_open_confirmation_shows_version_path_and_buttons(monkeypatch) -> None:
    monkeypatch.setattr(update_dialog, "QMessageBox", _FakeMessageBox)
    _FakeMessageBox.next_clicked_label = "Open Installer"
    dialog = _fake_dialog()
    installer_path = Path(r"C:\Users\tester\AppData\Local\Aura\updates\1.4.6\AuraSetup-1.4.6.exe")
    result = PullResult(
        True,
        None,
        target_version="1.4.6",
        installer_path=installer_path,
        launch_pending=True,
    )

    confirmed = dialog._confirm_open_installer(result)

    box = _FakeMessageBox.last
    assert confirmed is True
    assert box is not None
    assert box.title == "Open Aura Installer"
    assert "Aura 1.4.6" in box.text
    assert "Aura will close after launching the installer." in box.informative_text
    assert str(installer_path) in box.informative_text
    assert set(box.buttons) == {"Open Installer", "Cancel"}
    assert box.default_button == box.buttons["Open Installer"]


def test_installer_open_confirmation_cancel_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(update_dialog, "QMessageBox", _FakeMessageBox)
    _FakeMessageBox.next_clicked_label = "Cancel"
    dialog = _fake_dialog()
    result = PullResult(
        True,
        None,
        target_version="1.4.6",
        installer_path=Path(r"C:\Users\tester\AppData\Local\Aura\updates\1.4.6\AuraSetup-1.4.6.exe"),
        launch_pending=True,
    )

    assert dialog._confirm_open_installer(result) is False


def test_packaged_download_confirmation_cancel_keeps_aura_open(monkeypatch) -> None:
    monkeypatch.setattr(update_dialog, "is_packaged", lambda: True)
    dialog = _fake_dialog()
    launched: list[Path] = []
    installer_path = Path(r"C:\Users\tester\AppData\Local\Aura\updates\1.4.6\AuraSetup-1.4.6.exe")
    dialog._confirm_open_installer = lambda result: False
    monkeypatch.setattr(
        update_dialog,
        "launch_downloaded_installer",
        lambda path, **kwargs: launched.append(path),
    )

    dialog._show_pull_result(
        PullResult(
            True,
            None,
            message=f"Installer downloaded.\nDownloaded installer: {installer_path}",
            target_version="1.4.6",
            installer_path=installer_path,
            launch_pending=True,
        )
    )

    output = dialog._output.toPlainText()
    assert launched == []
    assert "Installer launch canceled" in output
    assert str(installer_path) in output


def test_packaged_download_confirmation_open_success_requests_app_quit(monkeypatch) -> None:
    monkeypatch.setattr(update_dialog, "is_packaged", lambda: True)
    dialog = _fake_dialog()
    installer_path = Path(r"C:\Users\tester\AppData\Local\Aura\updates\1.4.6\AuraSetup-1.4.6.exe")
    launch_calls: list[tuple[Path, str | None]] = []
    quit_calls: list[bool] = []
    dialog._confirm_open_installer = lambda result: True
    dialog._request_app_quit = lambda: quit_calls.append(True)

    def fake_launch(path: Path, output_callback=None, *, target_version: str | None = None) -> PullResult:
        launch_calls.append((path, target_version))
        return PullResult(
            True,
            None,
            message="Installer launched. Quitting Aura...",
            target_version=target_version,
            installer_path=path,
        )

    monkeypatch.setattr(update_dialog, "launch_downloaded_installer", fake_launch)

    dialog._show_pull_result(
        PullResult(
            True,
            None,
            message=f"Installer downloaded.\nDownloaded installer: {installer_path}",
            target_version="1.4.6",
            installer_path=installer_path,
            launch_pending=True,
        )
    )

    assert launch_calls == [(installer_path, "1.4.6")]
    assert quit_calls == [True]
    assert dialog._summary.text == "Installer launched. Aura will now exit to complete the update."


def test_packaged_download_confirmation_open_failure_keeps_aura_open(monkeypatch) -> None:
    monkeypatch.setattr(update_dialog, "is_packaged", lambda: True)
    dialog = _fake_dialog()
    installer_path = Path(r"C:\Users\tester\AppData\Local\Aura\updates\1.4.6\AuraSetup-1.4.6.exe")
    quit_calls: list[bool] = []
    dialog._confirm_open_installer = lambda result: True
    dialog._request_app_quit = lambda: quit_calls.append(True)

    def fake_launch(path: Path, output_callback=None, *, target_version: str | None = None) -> PullResult:
        return PullResult(
            False,
            None,
            message=(
                "Failed to launch the installer.\n"
                f"Downloaded installer: {path}\n"
                "Launch method: ShellExecuteW/open\n"
                f"If the installer does not appear, run this file manually: {path}"
            ),
            target_version=target_version,
            installer_path=path,
        )

    monkeypatch.setattr(update_dialog, "launch_downloaded_installer", fake_launch)

    dialog._show_pull_result(
        PullResult(
            True,
            None,
            message=f"Installer downloaded.\nDownloaded installer: {installer_path}",
            target_version="1.4.6",
            installer_path=installer_path,
            launch_pending=True,
        )
    )

    output = dialog._output.toPlainText()
    assert quit_calls == []
    assert str(installer_path) in output
    assert "Aura is still running" in output


def test_packaged_update_failure_does_not_request_app_quit(monkeypatch) -> None:
    monkeypatch.setattr(update_dialog, "is_packaged", lambda: True)
    dialog = _fake_dialog()

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
