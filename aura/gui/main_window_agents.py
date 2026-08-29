"""Agents responsibility cluster for MainWindow.

Phase 1 owns exactly one thing: opening and closing the Agents placeholder
page and keeping the rail entry's checked state honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from aura.gui.agents_page import AgentsPage

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow


class MainWindowAgentsController(QObject):
    """Owns the Agents page lifecycle for MainWindow."""

    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._agents_page: AgentsPage | None = None

    @property
    def agents_page(self) -> AgentsPage | None:
        return self._agents_page

    def is_open(self) -> bool:
        return bool(self._agents_page and self._agents_page.is_open())

    def hide_page(self) -> None:
        if self._agents_page is not None:
            self._agents_page.hide()

    def on_agents_requested(self) -> None:
        self.open_or_toggle_agents_page()

    def open_or_toggle_agents_page(self) -> None:
        if self._agents_page is None:
            self._agents_page = AgentsPage(self._window)
            self._agents_page.visibility_changed.connect(
                lambda _visible: self.sync_agents_tab_checked()
            )

        if self._agents_page.isVisible():
            self._agents_page.hide()
        else:
            self._agents_page.show()
            self._agents_page.raise_()
            self._agents_page.activateWindow()
        self.sync_agents_tab_checked()

    def sync_agents_tab_checked(self) -> None:
        rail = getattr(self._window, "_edge_rail", None)
        if rail is None:
            return
        tab = rail.agents_tab
        if tab is not None:
            tab.setChecked(self.is_open())
