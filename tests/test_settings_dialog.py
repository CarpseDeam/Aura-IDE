from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from aura.config import AppSettings
from aura.gui.left_pane import LeftPane
from aura.gui.settings_dialog import SettingsDialog


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_settings_dialog_preserves_max_tool_rounds(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(SettingsDialog, "_start_auth_status_check", lambda self: None)
    settings = AppSettings(max_tool_rounds=123)

    dlg = SettingsDialog(
        settings=settings,
        workspace_root=tmp_path,
        on_change_root=lambda: None,
    )

    assert dlg._settings.max_tool_rounds == 123
    assert dlg._max_rounds_spin.value() == 123
    assert dlg.result_settings().max_tool_rounds == 123
    dlg.close()


def test_settings_dialog_closes_with_auth_status_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    dlg = SettingsDialog(
        settings=AppSettings(),
        workspace_root=tmp_path,
        on_change_root=lambda: None,
    )

    dlg.close()
    qapp.processEvents()
