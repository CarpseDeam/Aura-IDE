from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from aura.config import AppSettings, save_settings
from aura.gui.settings_dialog import SettingsDialog

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class MainWindowSettingsController(QObject):
    """Owns the Settings/Application-State responsibility cluster for MainWindow.

    Stores a reference to the parent MainWindow and delegates to its
    attributes/methods for state apply, dialog creation, and toolbar sync.
    This avoids tight circular imports while keeping the extraction simple.
    """

    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window

    def open_settings(self) -> None:
        window = self._window
        try:
            dlg = SettingsDialog(
                settings=window._settings,
                workspace_root=window._workspace_root,
                on_change_root=window._workspace_controller.on_change_root,
                parent=window,
                on_live_settings_applied=self._apply_settings,
            )
            if dlg.exec() == SettingsDialog.DialogCode.Accepted:
                self._apply_settings(dlg.result_settings())
        except Exception:
            logger.exception("Failed to open Settings dialog")
            QMessageBox.critical(window, "Settings Error", "Could not open the Settings dialog. See the log for details.")

    def open_api_settings(self) -> None:
        """Open settings dialog directly to the API Keys tab."""
        window = self._window
        try:
            dlg = SettingsDialog(
                settings=window._settings,
                workspace_root=window._workspace_root,
                on_change_root=window._workspace_controller.on_change_root,
                parent=window,
                open_api_keys_tab=True,
                on_live_settings_applied=self._apply_settings,
            )
            if dlg.exec() == SettingsDialog.DialogCode.Accepted:
                self._apply_settings(dlg.result_settings())
        except Exception:
            logger.exception("Failed to open API Settings dialog")
            QMessageBox.critical(window, "Settings Error", "Could not open the API Settings dialog. See the log for details.")

    def open_credits_popout(self) -> None:
        """Open the standalone Aura Credits popout."""
        window = self._window
        try:
            from aura.gui.credits_popout import AuraCreditsPopoutDialog

            dlg = AuraCreditsPopoutDialog(
                settings=window._settings,
                parent=window,
            )
            dlg.credits_claimed.connect(lambda: window._balance_controller.refresh(window._settings))
            dlg.credits_claimed.connect(window._refresh_status_bar)
            dlg.credits_changed.connect(lambda: window._balance_controller.refresh(window._settings))
            dlg.credits_changed.connect(window._refresh_status_bar)
            dlg.exec()
            window._balance_controller.refresh(window._settings)
            window._refresh_status_bar()
        except Exception:
            logger.exception("Failed to open Aura Credits popout")
            QMessageBox.critical(window, "Aura Credits Error", "Could not open Aura Credits. See the log for details.")

    def open_aura_settings(self) -> None:
        """Backward-compatible route for callers that used to open the Aura settings tab."""
        self.open_credits_popout()

    def _apply_settings(self, settings: AppSettings) -> None:
        window = self._window
        window._settings = settings
        window._send_handler.update_settings(settings)
        window._companion_controller.update_settings(settings)
        window._persistence.update_settings(settings)
        window._worker_handler.update_settings(settings)
        window._toolbar.update_settings(settings)

        window._left_pane.populate_models(
            settings.planner_provider,
            settings.worker_provider,
        )
        window._bridge.set_planner_provider(settings.planner_provider)
        window._bridge.set_worker_provider(settings.worker_provider)

        if settings.planner_worker_mode:
            window.set_model(settings.default_planner_model)
            window.set_thinking(settings.default_planner_thinking)
        else:
            window.set_model(settings.default_model)
            window.set_thinking(settings.default_thinking)
        window.set_worker_model(settings.default_worker_model)
        window.set_worker_thinking(settings.default_worker_thinking)
        window._set_sidebar_planner_worker_mode(settings.planner_worker_mode)
        window._apply_planner_worker_mode_to_bridge(settings.planner_worker_mode)
        window._bridge.set_worker_model(settings.default_worker_model)
        window._bridge.set_worker_thinking(settings.default_worker_thinking)
        window._bridge.set_temperature(settings.temperature)
        window._bridge.set_worker_temperature(settings.worker_temperature)
        window._bridge.set_custom_system_prompts(
            settings.system_prompt,
            settings.planner_system_prompt,
            settings.worker_system_prompt,
        )
        window._bridge.set_auto_dispatch(settings.auto_dispatch)
        window._bridge.set_auto_approve(settings.auto_approve)
        window._toolbar.set_auto_dispatch(settings.auto_dispatch)
        window._toolbar.set_auto_approve(settings.auto_approve)
        window._toolbar.set_auto_summon_drones(settings.auto_summon_drones)
        window._refresh_status_bar()
        window._balance_controller.refresh(window._settings)

    def on_auto_dispatch_toggled(self, checked: bool) -> None:
        self._window._settings.auto_dispatch = checked
        self._window._bridge.set_auto_dispatch(checked)
        self._window._toolbar.refresh_auto_toggle_tooltips()
        save_settings(self._window._settings)

    def on_auto_approve_toggled(self, checked: bool) -> None:
        self._window._settings.auto_approve = checked
        self._window._bridge.set_auto_approve(checked)
        self._window._toolbar.refresh_auto_toggle_tooltips()
        save_settings(self._window._settings)

    def on_auto_summon_drones_toggled(self, checked: bool) -> None:
        self._window._settings.auto_summon_drones = checked
        self._window._toolbar.set_auto_summon_drones(checked)
        self._window._toolbar.refresh_auto_toggle_tooltips()
        save_settings(self._window._settings)
