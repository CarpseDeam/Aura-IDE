"""Companion popout — dedicated dialog opened from the rail phone badge."""
from __future__ import annotations

import copy
from typing import Callable

from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from aura.gui.settings_pages.companion_page import CompanionPage
from aura.settings import AppSettings


class CompanionPopoutDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        manager: object,
        on_apply: Callable[..., None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Aura Companion")
        self.setMinimumSize(420, 520)
        self.resize(460, 560)

        self._settings = copy.deepcopy(settings)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(0)

        self._page = CompanionPage(self._settings)
        self._page.set_manager(manager)
        layout.addWidget(self._page)

        self._page.apply_requested.connect(self._apply_live)

        self._on_apply = on_apply

    def _apply_live(self) -> None:
        result = self.collect_settings()
        if self._on_apply is not None:
            self._on_apply(result)

    def collect_settings(self) -> AppSettings:
        result = copy.deepcopy(self._settings)
        self._page.collect_settings(result)
        self._settings = result
        return result

    def accept(self) -> None:
        self._apply_live()
        super().accept()
