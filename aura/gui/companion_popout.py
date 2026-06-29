"""Companion popout — dedicated dialog opened from the rail phone badge."""
from __future__ import annotations

import copy
from typing import Callable

from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

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

        # Bottom button row
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 12, 0, 0)
        button_layout.setSpacing(8)

        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_live)
        button_layout.addWidget(apply_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close_with_save)
        button_layout.addWidget(close_btn)

        button_layout.addStretch()
        layout.addLayout(button_layout)

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
        self.close()

    def close_with_save(self) -> None:
        self.close()

    def closeEvent(self, event) -> None:
        self._apply_live()
        super().closeEvent(event)
