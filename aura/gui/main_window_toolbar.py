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
    update_requested = Signal()
    settings_requested = Signal()
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
        self._new_conv_btn.setText("New")
        self._new_conv_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._new_conv_btn.setToolTip("Start a new conversation (discards the current messages)")
        self._new_conv_btn.clicked.connect(self.new_conversation_requested.emit)
        self.addWidget(self._new_conv_btn)

        self._open_conv_btn = QToolButton()
        self._open_conv_btn.setIcon(QIcon(str(media_path("open_conversation.svg"))))
        self._open_conv_btn.setText("Open")
        self._open_conv_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._open_conv_btn.setToolTip("Open a previously saved conversation")
        self._open_conv_btn.clicked.connect(self.open_conversation_requested.emit)
        self.addWidget(self._open_conv_btn)

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
        self._auto_dispatch_switch = GlassSwitch("Dispatch", self._settings.auto_dispatch, vertical=True)
        self._auto_dispatch_switch.toggled.connect(self.auto_dispatch_toggled.emit)
        self.addWidget(self._auto_dispatch_switch)

        self.addWidget(_toolbar_separator())

        self._auto_approve_switch = GlassSwitch("Approve", self._settings.auto_approve, vertical=True)
        self._auto_approve_switch.toggled.connect(self.auto_approve_toggled.emit)
        self.addWidget(self._auto_approve_switch)
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

        # Settings button on the right side
        settings_act = QAction(QIcon(str(media_path("settings_24dp.svg"))), "Settings", self)
        settings_act.triggered.connect(self.settings_requested.emit)
        self.addAction(settings_act)

        # Small spacer before window controls.
        win_spacer = QWidget()
        win_spacer.setFixedWidth(8)
        self.addWidget(win_spacer)

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
            self._read_only_badge.setText("READ-ONLY")
        else:
            self._read_only_btn.setText("Read Only")
            self._read_only_btn.setToolTip("Toggle read-only mode to prevent file edits")
            self._read_only_badge.setText("")

    def _on_read_only_toggled(self, checked: bool) -> None:
        self._update_read_only_state(checked)
        self.read_only_toggled.emit(checked)

    def set_read_only(self, checked: bool) -> None:
        self._read_only_btn.blockSignals(True)
        self._read_only_btn.setChecked(checked)
        self._read_only_btn.blockSignals(False)
        self._update_read_only_state(checked)

    def update_settings(self, settings) -> None:
        """Use the latest settings object and refresh setting-backed controls."""
        self._settings = settings
        self.set_auto_dispatch(settings.auto_dispatch)
        self.set_auto_approve(settings.auto_approve)
        self.refresh_auto_toggle_tooltips()

    def set_auto_dispatch(self, checked: bool) -> None:
        self._auto_dispatch_switch.setChecked(checked)
        self.refresh_auto_toggle_tooltips()

    def set_auto_approve(self, checked: bool) -> None:
        self._auto_approve_switch.setChecked(checked)
        self.refresh_auto_toggle_tooltips()

    def refresh_auto_toggle_tooltips(self) -> None:
        self._auto_dispatch_switch.setToolTip(
            "Auto-dispatch: when ON, the planner sends tasks to the worker automatically. When OFF, the planner asks before dispatching."
        )
        self._auto_approve_switch.setToolTip(
            "Auto-approve: when ON, file diffs are applied without confirmation. When OFF, you review and approve each change."
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
