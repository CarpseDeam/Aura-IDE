from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import FG_DIM
from aura.gui.widgets.glass_switch import GlassSwitch
from aura.settings import AppSettings


class CompanionPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        title = QLabel("Companion (Mobile Control Plane)")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

        self._enabled_switch = GlassSwitch(
            "Enable Companion — connect to mobile web control plane",
            self._settings.companion_enabled,
        )
        form.addRow("", self._enabled_switch)

        self._display_name_edit = QLineEdit()
        self._display_name_edit.setPlaceholderText("Auto from hostname")
        self._display_name_edit.setText(self._settings.companion_display_name)
        form.addRow("Desktop Display Name:", self._display_name_edit)

        self._relay_url_edit = QLineEdit()
        self._relay_url_edit.setText(self._settings.companion_relay_url)
        form.addRow("Relay URL:", self._relay_url_edit)

        # Connection status indicator (wired to live status in Phase 1)
        self._status_label = QLabel("\u25cf Disabled")
        self._status_label.setStyleSheet(f"color: {FG_DIM};")
        form.addRow("Status:", self._status_label)

        layout.addLayout(form)
        layout.addStretch()

    def collect_settings(self, settings: AppSettings) -> None:
        settings.companion_enabled = self._enabled_switch.isChecked()
        settings.companion_display_name = self._display_name_edit.text()
        settings.companion_relay_url = self._relay_url_edit.text()
