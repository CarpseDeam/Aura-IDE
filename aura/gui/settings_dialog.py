"""Modal Settings dialog — tabbed shell with 7 page widgets."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aura.config import APP_NAME, AppSettings, save_settings

AURA_CREDITS_TAB_LABEL = "Credits & Account"


def _qt_tab_label(label: str) -> str:
    return label.replace("&", "&&")


class SettingsDialog(QDialog):
    """Modal settings.

    ``on_change_root`` is fired when the user clicks "Change..." next to the
    workspace label so the host (MainWindow) can run the same picker it uses
    elsewhere; we don't fork that logic here.
    """

    credits_claimed = Signal()

    def __init__(
        self,
        settings: AppSettings,
        workspace_root: Path | None,
        on_change_root: Callable[[], None],
        parent: QWidget | None = None,
        open_api_keys_tab: bool = False,
        open_aura_tab: bool = False,
        on_live_settings_applied: Callable[[AppSettings], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setModal(True)
        self.resize(640, 540)

        self._settings = copy.deepcopy(settings)
        self._on_live_settings_applied = on_live_settings_applied

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(14)

        self._tabs = QTabWidget(self)

        from aura.gui.settings_pages.api_keys_page import ApiKeysPage
        from aura.gui.settings_pages.aura_page import AuraPage
        from aura.gui.settings_pages.automation_page import AutomationPage
        from aura.gui.settings_pages.models_page import ModelsPage
        from aura.gui.settings_pages.prompts_page import PromptsPage
        from aura.gui.settings_pages.sandbox_page import SandboxPage
        self._models_page = ModelsPage(self._settings)

        self._aura_page = AuraPage(self._settings)
        self._aura_page.credits_claimed.connect(self.credits_claimed)

        self._api_keys_page = ApiKeysPage(self._settings)

        self._automation_page = AutomationPage(self._settings)

        self._sandbox_page = SandboxPage(self._settings, workspace_root, on_change_root)

        self._prompts_page = PromptsPage(self._settings)

        self._pages = [
            (self._models_page, "Models"),
            (self._aura_page, _qt_tab_label(AURA_CREDITS_TAB_LABEL)),
            (self._api_keys_page, "API Keys"),
            (self._automation_page, "Automation"),
            (self._sandbox_page, "Sandbox / Workspace"),
            (self._prompts_page, "Prompts"),
        ]

        from PySide6.QtWidgets import QScrollArea

        for page, label in self._pages:
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self._tabs.addTab(scroll, label)

        outer.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        if open_aura_tab:
            for i in range(self._tabs.count()):
                if self._tabs.tabText(i).replace("&&", "&") == AURA_CREDITS_TAB_LABEL:
                    self._tabs.setCurrentIndex(i)
                    break
        elif open_api_keys_tab:
            for i in range(self._tabs.count()):
                if self._tabs.tabText(i) == "API Keys":
                    self._tabs.setCurrentIndex(i)
                    break


    # --- Thread cleanup ---

    def _cleanup_threads(self) -> None:
        for page, _ in self._pages:
            if hasattr(page, "cleanup_threads"):
                page.cleanup_threads()

    def done(self, result: int) -> None:  # type: ignore[override]
        self._cleanup_threads()
        super().done(result)

    def closeEvent(self, event):  # type: ignore[override]
        self._cleanup_threads()
        super().closeEvent(event)

    # --- Result ---

    def result_settings(self) -> AppSettings:
        """Read the current widget values and return a fresh AppSettings.

        Uses a deep copy of the working settings as the base so that any
        field not managed by a settings page (e.g. aura_pending_*,
        main_window_geometry, main_window_state, main_splitter_sizes) is
        preserved rather than silently reset to its default value.
        """
        result = copy.deepcopy(self._settings)
        self._models_page.collect_settings(result)
        self._aura_page.collect_settings(result)
        self._api_keys_page.collect_settings(result)
        self._automation_page.collect_settings(result)
        self._sandbox_page.collect_settings(result)
        self._prompts_page.collect_settings(result)
        return result

    def accept(self) -> None:  # type: ignore[override]
        new_settings = self.result_settings()
        save_settings(new_settings)
        self._settings = new_settings
        super().accept()
