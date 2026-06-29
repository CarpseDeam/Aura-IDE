"""Companion popout — dedicated dialog opened from the rail phone badge."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from aura.gui.settings_pages.companion_page import CompanionPage
from aura.settings import AppSettings


class CompanionPopoutDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        manager: object,
        on_apply: callable | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Aura Companion")
        self.setMinimumSize(420, 520)
        self.resize(460, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(0)

        self._page = CompanionPage(settings)
        self._page.set_manager(manager)
        layout.addWidget(self._page)

        self._on_apply = on_apply

    def collect_settings(self) -> AppSettings:
        result = AppSettings()
        self._page.collect_settings(result)
        return result

    def accept(self) -> None:
        if self._on_apply is not None:
            self._on_apply(self.collect_settings())
        super().accept()
