from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from aura.config import AppSettings
from aura.gui.theme import FG_DIM
from aura.gui.widgets.glass_switch import GlassSwitch


class AutomationPage(QWidget):
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

        title = QLabel("Automation")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

        self._restore_chk = GlassSwitch(
            "Restore most-recent conversation on launch",
            self._settings.restore_last_conversation,
        )
        form.addRow("", self._restore_chk)

        self._auto_approve_chk = GlassSwitch(
            "Auto-approve: Apply file edits without diff approval",
            self._settings.auto_approve,
        )
        form.addRow("", self._auto_approve_chk)

        layout.addLayout(form)
        layout.addStretch()

    def collect_settings(self, settings: AppSettings) -> None:
        settings.restore_last_conversation = self._restore_chk.isChecked()
        settings.auto_approve = self._auto_approve_chk.isChecked()
