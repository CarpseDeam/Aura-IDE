from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from aura.config import AppSettings
from aura.gui.settings_dialog import SettingsDialog


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_settings_dialog_preserves_max_tool_rounds(
    qapp: QApplication, tmp_path: Path
) -> None:
    settings = AppSettings(max_tool_rounds=123)

    dlg = SettingsDialog(
        settings=settings,
        workspace_root=tmp_path,
        on_change_root=lambda: None,
    )

    assert dlg._settings.max_tool_rounds == 123
    assert dlg._automation_page._max_rounds_spin.value() == 123
    assert dlg.result_settings().max_tool_rounds == 123
    dlg.close()


def test_settings_dialog_closes_cleanly(
    qapp: QApplication, tmp_path: Path
) -> None:
    dlg = SettingsDialog(
        settings=AppSettings(),
        workspace_root=tmp_path,
        on_change_root=lambda: None,
    )

    dlg.close()
    qapp.processEvents()


def test_settings_dialog_construction_does_not_fetch_models(
    qapp: QApplication, tmp_path: Path
) -> None:
    with patch(
        "aura.gui.settings_pages.models_page.fetch_provider_models",
        return_value=([], None),
    ) as mock_fetch:
        dlg = SettingsDialog(
            settings=AppSettings(),
            workspace_root=tmp_path,
            on_change_root=lambda: None,
        )
        dlg.close()
        qapp.processEvents()

    mock_fetch.assert_called_once_with("deepseek")


def test_settings_dialog_construction_with_google_cloud_does_not_fetch_models(
    qapp: QApplication, tmp_path: Path
) -> None:
    with patch(
        "aura.gui.settings_pages.models_page.fetch_provider_models",
        return_value=([], None),
    ) as mock_fetch:
        dlg = SettingsDialog(
            settings=AppSettings(
                planner_provider="google_cloud", worker_provider="google_cloud"
            ),
            workspace_root=tmp_path,
            on_change_root=lambda: None,
        )
        dlg.close()
        qapp.processEvents()

    mock_fetch.assert_called_once_with("google_cloud")
