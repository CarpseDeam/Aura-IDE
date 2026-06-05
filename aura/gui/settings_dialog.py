"""Modal Settings dialog — tabbed shell with 7 page widgets."""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aura.config import APP_NAME, AppSettings, save_settings

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Modal settings.

    ``on_change_root`` is fired when the user clicks "Change..." next to the
    workspace label so the host (MainWindow) can run the same picker it uses
    elsewhere; we don't fork that logic here.
    """

    def __init__(
        self,
        settings: AppSettings,
        workspace_root: Path | None,
        on_change_root: Callable[[], None],
        parent: QWidget | None = None,
        open_api_keys_tab: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setModal(True)
        self.resize(640, 540)

        self._settings = copy.deepcopy(settings)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(14)

        self._tabs = QTabWidget(self)

        from aura.gui.settings_pages.models_page import ModelsPage
        from aura.gui.settings_pages.api_keys_page import ApiKeysPage
        from aura.gui.settings_pages.automation_page import AutomationPage
        from aura.gui.settings_pages.vision_page import VisionPage
        from aura.gui.settings_pages.sandbox_page import SandboxPage
        from aura.gui.settings_pages.prompts_page import PromptsPage

        self._models_page = ModelsPage(self._settings)
        self._api_keys_page = ApiKeysPage(self._settings)
        self._automation_page = AutomationPage(self._settings)
        self._vision_page = VisionPage(self._settings)
        self._sandbox_page = SandboxPage(self._settings, workspace_root, on_change_root)
        self._prompts_page = PromptsPage(self._settings)

        self._pages = [
            (self._models_page, "Models"),
            (self._api_keys_page, "API Keys"),
            (self._automation_page, "Automation"),
            (self._vision_page, "Vision"),
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

        if open_api_keys_tab:
            self._tabs.setCurrentIndex(1)

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
        """Read the current widget values and return a fresh AppSettings."""
        result = AppSettings(
            provider=self._settings.provider,
            default_model=self._settings.default_model,
            default_thinking=self._settings.default_thinking,
            show_planner_reasoning=self._settings.show_planner_reasoning,
            terminal_window_geometry=self._settings.terminal_window_geometry,
            first_launch_done=self._settings.first_launch_done,
            onboarding_checklist=dict(self._settings.onboarding_checklist),
            onboarding_version=self._settings.onboarding_version,
            humanizer_enabled=self._settings.humanizer_enabled,
            humanizer_gate_enabled=self._settings.humanizer_gate_enabled,
            humanizer_gate_min_severity=self._settings.humanizer_gate_min_severity,
            humanizer_feature_log=self._settings.humanizer_feature_log,
            humanizer_observe=self._settings.humanizer_observe,
        )

        self._models_page.collect_settings(result)
        self._api_keys_page.collect_settings(result)
        self._automation_page.collect_settings(result)
        self._vision_page.collect_settings(result)
        self._sandbox_page.collect_settings(result)
        self._prompts_page.collect_settings(result)

        return result

    def accept(self) -> None:  # type: ignore[override]
        new_settings = self.result_settings()
        save_settings(new_settings)
        self._settings = new_settings
        super().accept()
