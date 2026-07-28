"""Offscreen MainWindow/Settings lifecycle coverage after Credits removal."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox

from aura.gui.settings_dialog import SettingsDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_config(profile, provider: str = "deepseek", model: str = "deepseek-v4-flash") -> None:
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.json").write_text(
        json.dumps(
            {
                "provider": provider,
                "default_model": model,
                "first_launch_done": True,
                "restore_last_conversation": False,
            }
        ),
        encoding="utf-8",
    )


def _configure_profile(monkeypatch, tmp_path, provider="deepseek", model="deepseek-v4-flash"):
    profile = tmp_path / "profile"
    _write_config(profile, provider, model)
    monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
    monkeypatch.setenv("AURA_DATA_DIR", str(profile))
    return profile


def test_real_main_window_apply_has_no_credits_controller(monkeypatch, tmp_path) -> None:
    _app()
    _configure_profile(monkeypatch, tmp_path)
    from aura.gui.main_window import MainWindow

    window = MainWindow()
    try:
        assert not hasattr(window, "_balance_controller")
        updated = window._settings
        updated.max_tool_rounds += 1
        window._settings_controller._apply_settings(updated)
        assert window._settings.max_tool_rounds == updated.max_tool_rounds
    finally:
        window.close()
        window.deleteLater()


def test_settings_apply_accept_cancel_reopen_and_api_keys(
    monkeypatch, tmp_path
) -> None:
    _app()
    _configure_profile(monkeypatch, tmp_path)

    # Model discovery is unrelated to this lifecycle and can perform network I/O.
    from aura.gui.settings_pages.models_page import ModelsPage

    monkeypatch.setattr(ModelsPage, "_start_discovery", lambda self, _provider: None)

    from aura.gui.main_window import MainWindow

    critical_messages = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args: critical_messages.append(tuple(str(value) for value in args[1:])),
    )

    window = MainWindow()
    applied_rounds = window._settings.max_tool_rounds + 1
    window._settings.planner_system_prompt = "legacy planner prompt"
    window._settings.worker_system_prompt = "legacy worker prompt"

    def apply_and_accept() -> None:
        dialog = next(
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, SettingsDialog) and widget.isVisible()
        )
        assert not hasattr(dialog._prompts_page, "_planner_prompt_edit")
        assert not hasattr(dialog._prompts_page, "_worker_prompt_edit")
        dialog._automation_page._max_rounds_spin.setValue(applied_rounds)
        dialog._buttons.button(QDialogButtonBox.StandardButton.Apply).click()
        dialog._buttons.button(QDialogButtonBox.StandardButton.Ok).click()

    def cancel() -> None:
        dialog = next(
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, SettingsDialog) and widget.isVisible()
        )
        dialog._buttons.button(QDialogButtonBox.StandardButton.Cancel).click()

    try:
        QTimer.singleShot(0, apply_and_accept)
        window._settings_controller.open_settings()
        assert window._settings.max_tool_rounds == applied_rounds
        assert window._settings.planner_system_prompt == "legacy planner prompt"
        assert window._settings.worker_system_prompt == "legacy worker prompt"

        QTimer.singleShot(0, cancel)
        window._settings_controller.open_settings()

        QTimer.singleShot(0, cancel)
        window._settings_controller.open_api_settings()

        # Reopen once more after the API-key route has been torn down.
        QTimer.singleShot(0, cancel)
        window._settings_controller.open_settings()
        assert critical_messages == []
    finally:
        window.close()
        window.deleteLater()


def test_startup_shutdown_restart_with_fresh_and_old_aura_settings(
    monkeypatch, tmp_path
) -> None:
    _app()
    profile = _configure_profile(monkeypatch, tmp_path)
    from aura.gui.main_window import MainWindow

    first = MainWindow()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first._workspace_controller._retarget_workspace(
        workspace,
        restore_last=False,
    )
    assert first._workspace_root == workspace.resolve()
    first._on_new_conversation()
    assert first._persistence.current_conversation_path is None
    first.close()
    first.deleteLater()
    QApplication.processEvents()

    fixture = Path(__file__).parent / "fixtures" / "old_aura_config.json"
    (profile / "config.json").write_text(
        fixture.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    restarted = MainWindow()
    try:
        assert restarted._settings.provider != "aura"
        assert restarted._settings.default_model not in {"aura-fast", "aura-pro"}
        assert not hasattr(restarted, "_balance_controller")
    finally:
        restarted.close()
        restarted.deleteLater()
