from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QWidget,
)

from aura.config import media_path
from aura.gui.theme import LABEL_APPROVE, LABEL_DISPATCH, LABEL_DRONES, LABEL_READ_ONLY
from aura.gui.widgets.glass_switch import GlassSwitch


def _toolbar_separator() -> QFrame:
    sep = QFrame()
    sep.setObjectName("toolbarSeparator")
    sep.setFrameShape(QFrame.Shape.VLine)
    sep.setFrameShadow(QFrame.Shadow.Plain)
    sep.setFixedWidth(1)
    return sep

class MainWindowToolbar(QToolBar):
    new_conversation_requested = Signal()
    open_conversation_requested = Signal()
    read_only_toggled = Signal(bool)
    auto_dispatch_toggled = Signal(bool)
    auto_approve_toggled = Signal(bool)
    auto_summon_drones_toggled = Signal(bool)
    update_requested = Signal()
    settings_requested = Signal()
    logs_requested = Signal()
    debug_report_requested = Signal()
    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()
    def __init__(self, settings, parent=None) -> None:
        super().__init__("Main", parent)
        self.setMovable(False)
        self._settings = settings

        # Group 1: conversation actions
        self._new_conv_btn = QToolButton()
        self._new_conv_btn.setIcon(QIcon(str(media_path("new_conv.svg"))))
        self._new_conv_btn.setText("New Chat")
        self._new_conv_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._new_conv_btn.setToolTip("Start a new conversation (discards the current messages)")
        self._new_conv_btn.setStyleSheet("QToolButton { padding-left: 8px; padding-right: 8px; }")
        self._new_conv_btn.clicked.connect(self.new_conversation_requested.emit)
        new_conv_action = self.addWidget(self._new_conv_btn)
        new_conv_action.setText("New Conversation")
        new_conv_action.triggered.connect(self.new_conversation_requested.emit)

        self.addWidget(_toolbar_separator())

        # Group 2: read-only
        self._read_only_btn = QToolButton()
        self._read_only_btn.setIcon(QIcon(str(media_path("read_only.svg"))))
        self._read_only_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._read_only_btn.setCheckable(True)
        self._read_only_btn.setChecked(False)
        self._read_only_btn.toggled.connect(self._on_read_only_toggled)
        self.addWidget(self._read_only_btn)

        self._read_only_badge = QLabel("")
        self._read_only_badge.setObjectName("readOnlyBadge")
        self.addWidget(self._read_only_badge)

        self._update_read_only_state(False)

        self.addWidget(_toolbar_separator())

        # Group 3: auto toggles
        self._auto_dispatch_switch = GlassSwitch("Dispatch", self._settings.auto_dispatch, vertical=True, accent_color=LABEL_DISPATCH)
        self._auto_dispatch_switch.toggled.connect(self.auto_dispatch_toggled.emit)
        self.addWidget(self._auto_dispatch_switch)

        self.addWidget(_toolbar_separator())

        self._auto_approve_switch = GlassSwitch("Approve", self._settings.auto_approve, vertical=True, accent_color=LABEL_APPROVE)
        self._auto_approve_switch.toggled.connect(self.auto_approve_toggled.emit)
        self.addWidget(self._auto_approve_switch)

        self.addWidget(_toolbar_separator())

        self._auto_summon_drones_switch = GlassSwitch(
            "Drones",
            getattr(self._settings, "auto_summon_drones", False),
            vertical=True,
            accent_color=LABEL_DRONES,
        )
        self._auto_summon_drones_switch.toggled.connect(self.auto_summon_drones_toggled.emit)
        self.addWidget(self._auto_summon_drones_switch)
        self.refresh_auto_toggle_tooltips()

        # Icon-only style.
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        # Spacer.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addWidget(spacer)

        self._update_btn = QToolButton()
        self._update_btn.setText("Update")
        self._update_btn.setToolTip("Update Aura from GitHub")
        self._update_btn.clicked.connect(self.update_requested.emit)
        self.addWidget(self._update_btn)

        self.addWidget(_toolbar_separator())

        self._logs_btn = QToolButton()
        self._logs_btn.setText("Logs")
        self._logs_btn.setToolTip("Open Logs Folder")
        self._logs_btn.clicked.connect(self.logs_requested.emit)
        self.addWidget(self._logs_btn)

        self.addWidget(_toolbar_separator())

        self._debug_report_btn = QToolButton()
        self._debug_report_btn.setText("Send Logs")
        self._debug_report_btn.setToolTip("Send recent Aura logs to help diagnose problems")
        self._debug_report_btn.clicked.connect(self.debug_report_requested.emit)
        self.addWidget(self._debug_report_btn)

        # Spacer between Send Logs and Settings.
        settings_spacer = QWidget()
        settings_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addWidget(settings_spacer)

        # Settings button
        settings_act = QAction(QIcon(str(media_path("settings_24dp.svg"))), "Settings", self)
        settings_act.triggered.connect(self.settings_requested.emit)
        self.addAction(settings_act)

        self.addWidget(_toolbar_separator())

        # Window control buttons.
        min_btn = QToolButton()
        min_btn.setText("\u2500")  # ─
        min_btn.setObjectName("winMinBtn")
        min_btn.clicked.connect(self.minimize_requested.emit)
        self.addWidget(min_btn)

        self._max_btn = QToolButton()
        self._max_btn.setText("\u25a1")  # □
        self._max_btn.setObjectName("winMaxBtn")
        self._max_btn.clicked.connect(self.maximize_requested.emit)
        self.addWidget(self._max_btn)

        close_btn = QToolButton()
        close_btn.setText("\u2715")  # ✕
        close_btn.setObjectName("winCloseBtn")
        close_btn.clicked.connect(self.close_requested.emit)
        self.addWidget(close_btn)

    def _update_read_only_state(self, checked: bool) -> None:
        if checked:
            self._read_only_btn.setText("Read-Only")
            self._read_only_btn.setToolTip("Read-only mode is ON — files cannot be edited")
            self._read_only_btn.setStyleSheet(f"color: {LABEL_READ_ONLY};")
            self._read_only_badge.setText("READ-ONLY")
        else:
            self._read_only_btn.setText("Read Only")
            self._read_only_btn.setToolTip("Toggle read-only mode to prevent file edits")
            self._read_only_btn.setStyleSheet("")
            self._read_only_badge.setText("")

    def _on_read_only_toggled(self, checked: bool) -> None:
        self._update_read_only_state(checked)
        self.read_only_toggled.emit(checked)

    def set_read_only(self, checked: bool) -> None:
        self._read_only_btn.blockSignals(True)
        self._read_only_btn.setChecked(checked)
        self._read_only_btn.blockSignals(False)
        self._update_read_only_state(checked)

    def set_send_logs_busy(self, busy: bool) -> None:
        self._debug_report_btn.setEnabled(not busy)
        self._debug_report_btn.setText("Sending..." if busy else "Send Logs")
        self._debug_report_btn.setToolTip(
            "Sending recent Aura logs..." if busy else "Send recent Aura logs to help diagnose problems"
        )

    def update_settings(self, settings) -> None:
        """Use the latest settings object and refresh setting-backed controls."""
        self._settings = settings
        self.set_auto_dispatch(settings.auto_dispatch)
        self.set_auto_approve(settings.auto_approve)
        self.set_auto_summon_drones(getattr(settings, "auto_summon_drones", False))
        self.refresh_auto_toggle_tooltips()

    def set_auto_dispatch(self, checked: bool) -> None:
        self._auto_dispatch_switch.setChecked(checked)
        self.refresh_auto_toggle_tooltips()

    def set_auto_approve(self, checked: bool) -> None:
        self._auto_approve_switch.setChecked(checked)
        self.refresh_auto_toggle_tooltips()

    def set_auto_summon_drones(self, checked: bool) -> None:
        self._auto_summon_drones_switch.setChecked(checked)
        self.refresh_auto_toggle_tooltips()

    def refresh_auto_toggle_tooltips(self) -> None:
        self._auto_dispatch_switch.setToolTip(
            "Auto-dispatch: when ON, the planner sends tasks to the worker automatically. When OFF, the planner asks before dispatching."
        )
        self._auto_approve_switch.setToolTip(
            "Auto-approve: when ON, file diffs are applied without confirmation. When OFF, you review and approve each change."
        )
        self._auto_summon_drones_switch.setToolTip(
            "Auto-summon Drones: when ON, Aura launches suggested Drones without a confirmation card. When OFF, you approve each Drone summon."
        )

    def update_maximize_icon(self, maximized: bool) -> None:
        if maximized:
            self._max_btn.setText("\u2750")  # ❐
        else:
            self._max_btn.setText("\u25a1")  # □

    def set_update_available(self, available: bool) -> None:
        if available:
            self._update_btn.setStyleSheet("QToolButton { color: #ff9800; font-weight: bold; }")
            self._update_btn.setToolTip("A new version of Aura is available! Click to update.")
        else:
            self._update_btn.setStyleSheet("")
            self._update_btn.setToolTip("Update Aura from GitHub")
