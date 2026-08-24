from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer

from aura.config import save_settings

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow


class MainWindowTerminalController(QObject):
    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window

    def _on_terminal_toggle(self, checked: bool) -> None:
        self._window._playground.toggle_terminal_window()
        self._sync_terminal_checked_state()

    def _on_terminal_started(self) -> None:
        self._window._edge_rail.set_state("running")

    def _on_terminal_finished(self, exit_code: int) -> None:
        if exit_code == 0:
            self._window._edge_rail.set_state("success")
            QTimer.singleShot(1200, self._dim_terminal_tab_after_success)
        else:
            self._window._edge_rail.set_state("failure")

    def _on_terminal_visibility_changed(self, _visible: bool) -> None:
        self._sync_terminal_checked_state()
        self._window._edge_rail.set_is_terminal_open(self._window._playground.is_terminal_window_open())
        self._window._edge_rail.set_state(self._window._edge_rail.state)

    def _on_terminal_cleared(self) -> None:
        # Clearing the display while commands are still streaming says nothing
        # about the work itself, so the rail keeps its running state until the
        # remaining exit statuses arrive.
        if self._window._playground.terminal_window().has_active_commands:
            return
        self._window._edge_rail.set_state("dim")

    def _on_terminal_geometry_saved(self, geometry: str) -> None:
        if self._window._settings.terminal_window_geometry == geometry:
            return
        self._window._settings.terminal_window_geometry = geometry
        save_settings(self._window._settings)

    def _on_drone_reports_geometry_saved(self, geometry: str) -> None:
        if self._window._settings.drone_reports_window_geometry == geometry:
            return
        self._window._settings.drone_reports_window_geometry = geometry
        save_settings(self._window._settings)

    def _on_drone_workbay_geometry_saved(self, geometry: str) -> None:
        if self._window._settings.drone_workbay_window_geometry == geometry:
            return
        self._window._settings.drone_workbay_window_geometry = geometry
        save_settings(self._window._settings)

    def _dim_terminal_tab_after_success(self) -> None:
        if self._window._edge_rail.state == "success":
            self._window._edge_rail.set_state("dim")

    def _sync_terminal_checked_state(self) -> None:
        tab = self._window._edge_rail.terminal_tab
        if tab is None:
            return
        is_open = self._window._playground.is_terminal_window_open()
        tab.setChecked(is_open)
        self._window._edge_rail.set_is_terminal_open(is_open)
