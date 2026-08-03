"""Windows Computer Use settings — enable, status, and managed install.

Deliberately plain: an enable switch, a custom command, a status line, the
installed version and path, and three buttons.  Everything slow — resolving a
release, downloading, connecting — happens off the GUI thread; this page only
reads :meth:`WindowsComputerUseManager.status` snapshots and paints them.

Status is polled rather than signalled.  The manager is a plain object shared
with a worker thread, and a snapshot is immutable, so a 400 ms timer is both
correct across threads and considerably less machinery than marshalling
signals out of a non-Qt object.  The timer only runs while the page is visible.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import AppSettings
from aura.gui.theme import BORDER_STRONG, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN
from aura.gui.widgets.glass_switch import GlassSwitch
from aura.windows_mcp.manager import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISABLED,
    STATE_ERROR,
    WindowsComputerUseManager,
    WindowsComputerUseStatus,
)

_STATE_LABELS = {
    STATE_DISABLED: ("Disabled", FG_MUTED),
    STATE_CONNECTING: ("Connecting", WARN),
    STATE_CONNECTED: ("Connected", SUCCESS),
    STATE_ERROR: ("Error", DANGER),
}


class WindowsComputerUsePage(QWidget):
    apply_requested = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._manager: WindowsComputerUseManager | None = None
        self._action_thread: threading.Thread | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        header = QHBoxLayout()
        self._status_label = QLabel("● Disabled")
        self._status_label.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px; font-weight: 600;")
        header.addWidget(self._status_label)
        header.addStretch()
        self._enabled_switch = GlassSwitch(
            "Enable Windows Computer Use",
            getattr(settings, "windows_computer_use_enabled", False),
        )
        self._enabled_switch.toggled.connect(self._on_enable_toggled)
        header.addWidget(self._enabled_switch)
        outer.addLayout(header)

        blurb = QLabel(
            "Lets Aura drive Windows applications through structured UI "
            "Automation — finding, reading, clicking, and typing into real "
            "controls. No screenshots, no OCR tools, and no coordinate mouse "
            "or raw keyboard control are exposed. Actions that change another "
            "application go through the normal approval prompt."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        outer.addWidget(blurb)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self._command_edit = QLineEdit(
            getattr(settings, "windows_computer_use_command", "")
        )
        self._command_edit.setPlaceholderText(
            "Leave empty to use Aura's managed install"
        )
        self._command_edit.editingFinished.connect(self._on_command_edited)
        form.addRow(self._dim_label("Custom command"), self._command_edit)

        self._version_label = QLabel("Not installed")
        self._version_label.setStyleSheet(f"color: {FG}; font-size: 11px;")
        form.addRow(self._dim_label("Installed version"), self._version_label)

        self._path_label = QLabel("—")
        self._path_label.setStyleSheet(
            f"color: {FG_DIM}; font-size: 10px;"
            " font-family: 'JetBrains Mono', 'Consolas', monospace;"
        )
        self._path_label.setWordWrap(True)
        self._path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow(self._dim_label("Path"), self._path_label)

        outer.addLayout(form)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._install_btn = self._ghost_button("Install", self._on_install)
        self._repair_btn = self._ghost_button("Repair", self._on_repair)
        self._remove_btn = self._ghost_button("Remove", self._on_remove)
        buttons.addWidget(self._install_btn)
        buttons.addWidget(self._repair_btn)
        buttons.addWidget(self._remove_btn)
        buttons.addStretch()
        outer.addLayout(buttons)

        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        outer.addWidget(self._detail_label)

        outer.addStretch()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(400)
        self._poll_timer.timeout.connect(self._refresh_status)

    # ── manager wiring ──────────────────────────────────────────────────

    def set_manager(self, manager: WindowsComputerUseManager) -> None:
        self._manager = manager
        self._refresh_status()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._refresh_status()
        self._poll_timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._poll_timer.stop()
        super().hideEvent(event)

    def cleanup_threads(self) -> None:
        """Called by the dialog on close; installs are allowed to finish."""
        self._poll_timer.stop()

    # ── slots ───────────────────────────────────────────────────────────

    def _on_enable_toggled(self, _checked: bool) -> None:
        self._sync_buttons()
        self.apply_requested.emit()

    def _on_command_edited(self) -> None:
        if self._enabled_switch.isChecked():
            self.apply_requested.emit()

    def _on_install(self) -> None:
        self._run_action(lambda mgr: mgr.install_or_repair(force=False))

    def _on_repair(self) -> None:
        self._run_action(lambda mgr: mgr.install_or_repair(force=True))

    def _on_remove(self) -> None:
        self._run_action(lambda mgr: mgr.remove_installation())

    def _run_action(self, action) -> None:
        """Run a managed-install action off the GUI thread."""
        manager = self._manager
        if manager is None:
            return
        if self._action_thread is not None and self._action_thread.is_alive():
            return
        self._set_buttons_enabled(False)
        self._detail_label.setText("Working...")

        def _work() -> None:
            try:
                action(manager)
            finally:
                pass

        self._action_thread = threading.Thread(
            target=_work, name="windows-mcp-settings-action", daemon=True
        )
        self._action_thread.start()
        self._poll_timer.start()

    # ── painting ────────────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        manager = self._manager
        if manager is None:
            return
        status = manager.status()
        self._paint(status)
        busy = self._action_thread is not None and self._action_thread.is_alive()
        if not busy:
            self._set_buttons_enabled(True)
            self._sync_buttons()

    def _paint(self, status: WindowsComputerUseStatus) -> None:
        label, color = _STATE_LABELS.get(status.state, ("Unknown", FG_DIM))
        if status.state == STATE_CONNECTED and status.tool_count:
            label = f"Connected — {status.tool_count} tools"
        self._status_label.setText(f"● {label}")
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600;"
        )

        self._version_label.setText(status.version or "Not installed")
        self._path_label.setText(status.path or "—")

        if status.error:
            self._detail_label.setText(status.error)
            self._detail_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        else:
            self._detail_label.setText(status.detail)
            self._detail_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")

    def _sync_buttons(self) -> None:
        """A custom command owns the whole path, so managed actions go away."""
        managed = not self._command_edit.text().strip()
        for button in (self._install_btn, self._repair_btn, self._remove_btn):
            button.setVisible(managed)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for button in (self._install_btn, self._repair_btn, self._remove_btn):
            button.setEnabled(enabled)

    # ── settings ────────────────────────────────────────────────────────

    def collect_settings(self, settings: AppSettings) -> None:
        settings.windows_computer_use_enabled = self._enabled_switch.isChecked()
        settings.windows_computer_use_command = self._command_edit.text().strip()

    # ── style helpers ───────────────────────────────────────────────────

    def _ghost_button(self, text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            "QPushButton {"
            f"  background: transparent; color: {FG};"
            f"  border: 1px solid {BORDER_STRONG}; border-radius: 8px;"
            "  padding: 6px 14px; font-weight: 500;"
            "}"
            f"QPushButton:disabled {{ color: {FG_MUTED}; }}"
        )
        button.clicked.connect(slot)
        return button

    @staticmethod
    def _dim_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        return label
