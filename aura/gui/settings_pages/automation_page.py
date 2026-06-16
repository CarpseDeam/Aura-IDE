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

        self._auto_dispatch_chk = GlassSwitch(
            "Auto-dispatch: Send specs to worker without approval",
            self._settings.auto_dispatch,
        )
        form.addRow("", self._auto_dispatch_chk)

        self._auto_approve_chk = GlassSwitch(
            "Auto-approve: Apply file edits without diff approval",
            self._settings.auto_approve,
        )
        form.addRow("", self._auto_approve_chk)

        self._auto_summon_drones_chk = GlassSwitch(
            "Auto-summon Drones: Launch suggested Drones without approval",
            getattr(self._settings, "auto_summon_drones", False),
        )
        form.addRow("", self._auto_summon_drones_chk)

        self._max_rounds_spin = QSpinBox()
        self._max_rounds_spin.setRange(1, 500)
        self._max_rounds_spin.setToolTip(
            "Maximum number of tool-call rounds allowed in a single user turn."
        )
        self._max_rounds_spin.setValue(self._settings.max_tool_rounds)
        form.addRow("Max tool rounds:", self._max_rounds_spin)

        layout.addLayout(form)
        layout.addStretch()

    def collect_settings(self, settings: AppSettings) -> None:
        settings.restore_last_conversation = self._restore_chk.isChecked()
        settings.auto_dispatch = self._auto_dispatch_chk.isChecked()
        settings.auto_approve = self._auto_approve_chk.isChecked()
        settings.auto_summon_drones = self._auto_summon_drones_chk.isChecked()
        settings.max_tool_rounds = self._max_rounds_spin.value()
