"""Modeless Drone Workbay window — card menu of saved Drones."""

from __future__ import annotations

import json
import time
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dataclasses import replace

from aura.drones.store import DroneStore
from aura.gui.theme import ACCENT, BG, BG_ALT, BORDER, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 5000

STATUS_COLORS = {
    "Ready": SUCCESS,
    "Needs Fix": DANGER,
    "Building": WARN,
    "Testing": WARN,
    "Draft": FG_MUTED,
}


def _qss_color(hex_str: str) -> str:
    c = QColor(hex_str)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha() / 255:.2f})"


class _DroneCard(QFrame):
    """Card widget showing one Drone's info and action buttons."""

    def __init__(
        self,
        drone_id: str,
        name: str,
        description: str,
        write_policy: str = "read_only",
        status: str = "Ready",
        loop_enabled: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.drone_id = drone_id
        self._loop_enabled = loop_enabled
        self.loop_toggled = None  # callback(drone_id, enabled, interval_seconds)
        self.interval_changed = None  # callback(drone_id, interval_seconds)

        self.setObjectName("workbay_card")
        self.setStyleSheet(
            "#workbay_card {"
            "  background: rgba(18, 20, 28, 0.90);"
            "  border: 1px solid rgba(255, 255, 255, 0.06);"
            "  border-radius: 10px;"
            "}"
            "#workbay_card:hover {"
            "  border-color: rgba(196, 181, 253, 0.20);"
            "  background: rgba(24, 26, 36, 0.94);"
            "}"
        )
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Header row: status dot + name + Loop toggle
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        dot_color = STATUS_COLORS.get(status, FG_MUTED)
        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        self._dot.setStyleSheet(
            f"background: {dot_color};"
            f"border-radius: 5px;"
            f"border: none;"
        )
        header_row.addWidget(self._dot)

        title = QLabel(name)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPixelSize(13)
        title.setFont(title_font)
        title.setStyleSheet(
            f"color: {_qss_color(FG)}; background: transparent; border: none;"
        )
        header_row.addWidget(title, 1)

        loop_label = QLabel("Loop")
        loop_label.setStyleSheet(
            f"color: {_qss_color(FG_DIM)}; font-size: 11px; background: transparent; border: none;"
        )
        header_row.addWidget(loop_label)

        self._loop_toggle = QCheckBox()
        self._loop_toggle.setChecked(loop_enabled)
        self._loop_toggle.setStyleSheet(
            "QCheckBox::indicator {"
            "  width: 34px; height: 18px;"
            "  border-radius: 9px;"
            "  border: 1px solid rgba(255,255,255,0.12);"
            "  background: rgba(255,255,255,0.08);"
            "}"
            "QCheckBox::indicator:checked {"
            "  background: #9ece6a;"
            "  border-color: rgba(158,206,106,0.4);"
            "}"
            "QCheckBox::indicator:disabled {"
            "  opacity: 0.4;"
            "}"
        )
        self._loop_toggle.stateChanged.connect(self._on_loop_toggled)
        header_row.addWidget(self._loop_toggle)

        layout.addLayout(header_row)

        # Pill row: status + policy
        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 0, 0, 0)
        pill_row.setSpacing(6)

        status_color = STATUS_COLORS.get(status, FG_MUTED)
        self._status_pill = QLabel(status)
        self._status_pill.setStyleSheet(
            f"color: {status_color};"
            f"background: rgba(255, 255, 255, 0.05);"
            f"border: 1px solid rgba(255, 255, 255, 0.08);"
            f"border-radius: 3px;"
            f"padding: 1px 6px;"
            f"font-size: 10px;"
        )
        self._status_pill.setFixedHeight(16)
        pill_row.addWidget(self._status_pill)

        policy_text = "read-only" if write_policy == "read_only" else "writes"
        policy_color = "#7dcfff" if write_policy == "read_only" else WARN
        policy_pill = QLabel(policy_text)
        policy_pill.setStyleSheet(
            f"color: {policy_color};"
            f"background: rgba(255, 255, 255, 0.05);"
            f"border: 1px solid rgba(255, 255, 255, 0.08);"
            f"border-radius: 3px;"
            f"padding: 1px 6px;"
            f"font-size: 10px;"
        )
        policy_pill.setFixedHeight(16)
        pill_row.addWidget(policy_pill)

        pill_row.addStretch()
        layout.addLayout(pill_row)

        # Run state indicator (hidden by default)
        self._run_state_label = QLabel()
        self._run_state_label.setMaximumHeight(32)
        self._run_state_label.setStyleSheet(
            "font-size: 10px; background: transparent; border: none; padding: 0px;"
        )
        self._run_state_label.hide()
        layout.addWidget(self._run_state_label)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._update_countdown)
        self._wait_until: float = 0.0

        # Description (max 2 lines)
        self._desc = QLabel(description if description else "")
        self._desc.setWordWrap(True)
        self._desc.setMaximumHeight(36)
        self._desc.setStyleSheet(
            f"color: {_qss_color(FG_MUTED)}; font-size: 11px; background: transparent; border: none;"
        )
        self._desc.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._desc)

        # Loop interval row (hidden when loop is OFF)
        self._interval_container = QWidget()
        interval_layout = QHBoxLayout(self._interval_container)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.setSpacing(6)

        every_label = QLabel("Every")
        every_label.setStyleSheet(
            f"color: {_qss_color(FG_DIM)}; font-size: 11px; background: transparent; border: none;"
        )
        interval_layout.addWidget(every_label)

        _SPIN_STYLE = (
            "QSpinBox {"
            "  background: rgba(255,255,255,0.05);"
            "  border: 1px solid rgba(255,255,255,0.08);"
            "  border-radius: 4px;"
            "  color: #eaecef;"
            "  padding: 2px 6px;"
            "  font-size: 11px;"
            "  max-width: 60px;"
            "}"
            "QSpinBox::up-button, QSpinBox::down-button {"
            "  width: 14px;"
            "  border: none;"
            "  background: transparent;"
            "}"
            "QSpinBox::up-arrow { image: none; }"
            "QSpinBox::down-arrow { image: none; }"
        )

        self._interval_minutes_spin = QSpinBox()
        self._interval_minutes_spin.setRange(0, 999)
        self._interval_minutes_spin.setValue(1)
        self._interval_minutes_spin.setStyleSheet(_SPIN_STYLE)
        interval_layout.addWidget(self._interval_minutes_spin)

        min_label = QLabel("min")
        min_label.setStyleSheet(
            f"color: {_qss_color(FG_DIM)}; font-size: 11px; background: transparent; border: none;"
        )
        interval_layout.addWidget(min_label)

        self._interval_seconds_spin = QSpinBox()
        self._interval_seconds_spin.setRange(0, 59)
        self._interval_seconds_spin.setValue(0)
        self._interval_seconds_spin.setStyleSheet(_SPIN_STYLE)
        interval_layout.addWidget(self._interval_seconds_spin)

        sec_label = QLabel("sec")
        sec_label.setStyleSheet(
            f"color: {_qss_color(FG_DIM)}; font-size: 11px; background: transparent; border: none;"
        )
        interval_layout.addWidget(sec_label)

        self._interval_minutes_spin.valueChanged.connect(self._on_interval_changed)
        self._interval_seconds_spin.valueChanged.connect(self._on_interval_changed)

        interval_layout.addStretch()

        self._dim_effect = QGraphicsOpacityEffect(self._interval_container)
        self._dim_effect.setOpacity(1.0 if loop_enabled else 0.5)
        self._interval_container.setGraphicsEffect(self._dim_effect)
        layout.addWidget(self._interval_container)

        # Action row
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        # Run
        self._btn_run = QPushButton("\u25b6 Run")
        self._btn_run.setStyleSheet(
            f"QPushButton {{"
            f"  background: rgba(196, 181, 253, 0.12);"
            f"  border: 1px solid rgba(196, 181, 253, 0.18);"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(ACCENT)};"
            f"  padding: 3px 12px;"
            f"  font-size: 11px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(196, 181, 253, 0.22);"
            f"  border-color: rgba(196, 181, 253, 0.35);"
            f"  color: {_qss_color(FG)};"
            f"}}"
        )
        self._btn_run.setCursor(Qt.PointingHandCursor)
        action_row.addWidget(self._btn_run)

        action_row.addStretch()

        # Delete
        self._btn_delete = QPushButton("\U0001f5d1 Delete")
        self._btn_delete.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid rgba(248, 113, 113, 0.15);"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(DANGER)};"
            f"  font-size: 11px;"
            f"  padding: 3px 12px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(248, 113, 113, 0.12);"
            f"  border-color: rgba(248, 113, 113, 0.30);"
            f"}}"
        )
        self._btn_delete.setCursor(Qt.PointingHandCursor)
        action_row.addWidget(self._btn_delete)

        layout.addLayout(action_row)

    @property
    def loop_enabled(self) -> bool:
        return self._loop_toggle.isChecked()

    def _on_loop_toggled(self, state: int) -> None:
        enabled = bool(state)
        self._dim_effect.setOpacity(1.0 if enabled else 0.5)
        if self.loop_toggled is not None:
            self.loop_toggled(self.drone_id, enabled, self.interval_seconds)

    @property
    def interval_seconds(self) -> int:
        return max(5, self._interval_minutes_spin.value() * 60 + self._interval_seconds_spin.value())

    @interval_seconds.setter
    def interval_seconds(self, seconds: int) -> None:
        clamped = max(5, seconds)
        mins = clamped // 60
        secs = clamped % 60
        self._interval_minutes_spin.blockSignals(True)
        self._interval_seconds_spin.blockSignals(True)
        self._interval_minutes_spin.setValue(mins)
        self._interval_seconds_spin.setValue(secs)
        self._interval_minutes_spin.blockSignals(False)
        self._interval_seconds_spin.blockSignals(False)

    def _on_interval_changed(self) -> None:
        if self._loop_toggle.isChecked() and self.interval_changed is not None:
            self.interval_changed(self.drone_id, self.interval_seconds)

    def update_status(self, status: str) -> None:
        """Update the status dot and pill without recreating the card."""
        color = STATUS_COLORS.get(status, FG_MUTED)
        self._dot.setStyleSheet(f"background: {color}; border-radius: 5px; border: none;")
        self._status_pill.setText(status)
        self._status_pill.setStyleSheet(
            f"color: {color};"
            f"background: rgba(255, 255, 255, 0.05);"
            f"border: 1px solid rgba(255, 255, 255, 0.08);"
            f"border-radius: 3px;"
            f"padding: 1px 6px;"
            f"font-size: 10px;"
        )

    # -- Run state indicator ------------------------------------------------

    def set_run_state(self, state: str, detail: str = "") -> None:
        """Update the run state indicator label."""
        if state == "idle":
            self._run_state_label.hide()
            self._countdown_timer.stop()
        elif state == "running":
            self._run_state_label.setText("Running lap...")
            self._run_state_label.setStyleSheet(
                "font-size: 10px; background: transparent; border: none; padding: 0px;"
                "color: #4ec9b0; font-weight: bold;"
            )
            self._run_state_label.show()
            self._countdown_timer.stop()
        elif state == "completed":
            self._run_state_label.setText(f"Last run: {detail}")
            self._run_state_label.setStyleSheet(
                f"font-size: 10px; background: transparent; border: none; padding: 0px;"
                f"color: {SUCCESS};"
            )
            self._run_state_label.show()
            self._countdown_timer.stop()
        elif state == "failed":
            self._run_state_label.setText(f"Failed: {detail}")
            self._run_state_label.setStyleSheet(
                f"font-size: 10px; background: transparent; border: none; padding: 0px;"
                f"color: {DANGER};"
            )
            self._run_state_label.show()
            self._countdown_timer.stop()
        elif state == "waiting_for_loop":
            remaining = float(detail) if detail else 0
            self._wait_until = time.time() + remaining
            self._update_countdown()
            self._run_state_label.show()
            self._countdown_timer.start()

    def _update_countdown(self) -> None:
        remaining = max(0, self._wait_until - time.time())
        if remaining <= 0:
            self._countdown_timer.stop()
            self._run_state_label.hide()
            return
        if remaining < 60:
            self._run_state_label.setText(f"Waiting for next loop: {int(remaining)}s")
        else:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            self._run_state_label.setText(f"Waiting for next loop: {mins}m {secs}s")
        self._run_state_label.setStyleSheet(
            f"font-size: 10px; background: transparent; border: none; padding: 0px;"
            f"color: {WARN};"
        )
        self._run_state_label.show()


class _NewBrowseMonitorDialog(QDialog):
    """Dialog to configure a new Browse Monitor drone."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Browse Monitor")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setSpacing(8)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Home Page Monitor")
        form.addRow("Name:", self._name_edit)

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://example.com")
        form.addRow("Start URL:", self._url_edit)

        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("e.g. home_page")
        form.addRow("Monitor Key:", self._key_edit)

        self._fields_edit = QLineEdit("title, url, body_excerpt")
        form.addRow("Monitor Fields:", self._fields_edit)

        self._chars_spin = QSpinBox()
        self._chars_spin.setRange(100, 10000)
        self._chars_spin.setValue(2000)
        form.addRow("Excerpt Chars:", self._chars_spin)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def name_value(self) -> str:
        return self._name_edit.text().strip()

    @property
    def start_url_value(self) -> str:
        return self._url_edit.text().strip()

    @property
    def monitor_key_value(self) -> str:
        return self._key_edit.text().strip()

    @property
    def monitor_fields_value(self) -> list[str]:
        raw = self._fields_edit.text().strip()
        return [f.strip() for f in raw.split(",") if f.strip()]

    @property
    def excerpt_chars_value(self) -> int:
        return self._chars_spin.value()


class DroneWorkbayWindow(QDialog):
    """Non-modal window showing a scrollable card menu of saved Drones.

    Hiding this window preserves the card list state. WA_DeleteOnClose
    is False so closing via the WM close button only hides the window.
    """

    geometry_saved = Signal(str)

    runDroneRequested = Signal(str, str)  # drone_id, folder
    deleteDroneRequested = Signal(str)    # drone_id
    loopDroneRequested = Signal(str, bool, int)  # drone_id, enabled, interval_seconds
    loopIntervalChanged = Signal(str, int)  # drone_id, interval_seconds

    def __init__(
        self,
        workspace_root: Path | None = None,
        initial_geometry: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drone Workbay")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.resize(560, 600)
        self.setMinimumSize(400, 400)

        self._workspace_root = workspace_root or Path.cwd()
        self._geometry_restore_done = False
        self._initial_geometry = initial_geometry.strip()
        self._cards: dict[str, _DroneCard] = {}
        self._loop_intervals: dict[str, int] = {}

        # Geometry save timer
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(250)
        self._geometry_save_timer.timeout.connect(self._save_geometry)

        # Periodic refresh timer
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_status)
        self._poll_timer.start()

        self._build_ui()

        # Wire New Browse Monitor button
        self._btn_new_browse.clicked.connect(self._on_new_browse_monitor)

        self._restore_geometry(self._initial_geometry)
        self._geometry_restore_done = True

    def _on_new_browse_monitor(self) -> None:
        """Open the New Browse Monitor dialog and create the drone on accept."""
        dlg = _NewBrowseMonitorDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                DroneStore.create_browse_monitor(
                    workspace_root=self._workspace_root,
                    name=dlg.name_value,
                    start_url=dlg.start_url_value,
                    monitor_key=dlg.monitor_key_value,
                    monitor_fields=dlg.monitor_fields_value,
                    monitor_excerpt_chars=dlg.excerpt_chars_value,
                )
                self.refresh()
            except Exception as exc:
                logger.error("Failed to create browse monitor: %s", exc)

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background: {_qss_color(BG_ALT)}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QLabel("Drone Workbay")
        header_font = QFont()
        header_font.setBold(True)
        header_font.setPixelSize(16)
        header.setFont(header_font)
        header.setStyleSheet(
            f"color: {_qss_color(FG)};"
            f"padding: 14px 16px 6px 16px;"
            f"background: transparent;"
            f"border: none;"
        )
        root.addWidget(header)

        # Scrollable card list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{"
            f"  background: {_qss_color(BG)}; width: 6px; border-radius: 3px;"
            f"}}"
            f"QScrollBar::handle:vertical {{"
            f"  background: {_qss_color(BORDER)}; border-radius: 3px; min-height: 20px;"
            f"}}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
        )

        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._card_layout = QVBoxLayout(self._content)
        self._card_layout.setContentsMargins(12, 4, 12, 12)
        self._card_layout.setSpacing(8)
        self._card_layout.addStretch()

        scroll.setWidget(self._content)
        root.addWidget(scroll, 1)

        # New Browse Monitor button
        self._btn_new_browse = QPushButton("+ New Browse Monitor")
        self._btn_new_browse.setCursor(Qt.PointingHandCursor)
        self._btn_new_browse.setStyleSheet(
            f"QPushButton {{"
            f"  background: rgba(122, 162, 247, 0.10);"
            f"  border: 1px solid rgba(122, 162, 247, 0.25);"
            f"  border-radius: 6px;"
            f"  color: #7aa2f7;"
            f"  font-size: 12px;"
            f"  padding: 8px 16px;"
            f"  margin: 0px 12px 12px 12px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(122, 162, 247, 0.18);"
            f"  border-color: rgba(122, 162, 247, 0.40);"
            f"}}"
        )
        root.addWidget(self._btn_new_browse)

    # -- Public API ---------------------------------------------------------

    def set_workspace_root(self, root: Path) -> None:
        self._workspace_root = root

    def refresh(self) -> None:
        """Reload the drone list from DroneStore and rebuild cards."""
        self._workspace_root = self._workspace_root or Path.cwd()
        drones = DroneStore.list_drones(self._workspace_root)

        # Determine status for each drone by reading drone.json directly
        status_map: dict[str, str] = {}
        for d in drones:
            folder = DroneStore.drone_folder(self._workspace_root, d.id)
            status_map[d.id] = self._detect_status(folder)

        # Track which ids still exist
        current_ids = {d.id for d in drones}

        # Remove cards for drones that no longer exist
        for card_id in list(self._cards.keys()):
            if card_id not in current_ids:
                card = self._cards.pop(card_id)
                self._card_layout.removeWidget(card)
                card.deleteLater()

        # Rebuild or update cards
        for d in drones:
            status = status_map.get(d.id, "Ready")
            # Determine loop state from persisted permissions
            perm_loop_enabled = bool(d.permissions.get("loop_enabled", False))
            perm_interval = d.permissions.get("loop_interval_seconds")
            if perm_interval is None:
                perm_interval = 3600 if d.kind == "browse" else 60
            if d.id in self._cards:
                card = self._cards[d.id]
                card.update_status(status)
            else:
                card = _DroneCard(
                    drone_id=d.id,
                    name=d.name,
                    description=d.description,
                    write_policy=d.write_policy,
                    status=status,
                    loop_enabled=perm_loop_enabled,
                )
                card._btn_run.clicked.connect(
                    lambda checked, did=d.id: self.runDroneRequested.emit(did, "")
                )
                card._btn_delete.clicked.connect(
                    lambda checked, did=d.id: self.deleteDroneRequested.emit(did)
                )
                # Loop wiring via callbacks
                card.loop_toggled = lambda did, enabled, interval: self._on_card_loop_toggled(did, enabled, interval)
                card.interval_changed = lambda did, interval: self._on_card_loop_interval_changed(did, interval)
                self._cards[d.id] = card
                card.set_run_state("idle")
                self._card_layout.insertWidget(self._card_layout.count() - 1, card)
            # Apply loop state from permissions for all cards
            if perm_loop_enabled:
                card.interval_seconds = perm_interval
                card._loop_toggle.blockSignals(True)
                card._loop_toggle.setChecked(True)
                card._loop_toggle.blockSignals(False)
                self._loop_intervals[d.id] = perm_interval

    def set_card_loop_state(self, drone_id: str, enabled: bool, interval_seconds: int = 0) -> None:
        """Update the loop state on a drone card."""
        card = self._cards.get(drone_id)
        if card is not None:
            card._loop_toggle.setChecked(enabled)
            if card._dim_effect is not None:
                card._dim_effect.setOpacity(1.0 if enabled else 0.5)
            if interval_seconds > 0:
                card.interval_seconds = interval_seconds
            if enabled:
                self._loop_intervals[drone_id] = interval_seconds or card.interval_seconds
            else:
                self._loop_intervals.pop(drone_id, None)

    def set_card_run_state(self, drone_id: str, state: str, detail: str = "") -> None:
        """Update the run state indicator on a drone card."""
        card = self._cards.get(drone_id)
        if card is not None:
            card.set_run_state(state, detail)

    def _persist_loop_state(self, drone_id: str, enabled: bool, interval_seconds: int) -> None:
        """Persist the loop toggle and interval to the drone's permissions on disk."""
        try:
            drone = DroneStore.load_drone(self._workspace_root, drone_id)
            if drone is None:
                return
            updated = replace(
                drone,
                permissions={
                    **drone.permissions,
                    "loop_enabled": enabled,
                    "loop_interval_seconds": interval_seconds,
                },
            )
            DroneStore.save_drone(self._workspace_root, updated)
        except Exception as exc:
            logger.warning("Failed to persist loop state for %s: %s", drone_id, exc)

    def _on_card_loop_toggled(self, drone_id: str, enabled: bool, interval_seconds: int) -> None:
        self._loop_intervals[drone_id] = interval_seconds
        self._persist_loop_state(drone_id, enabled, interval_seconds)
        self.loopDroneRequested.emit(drone_id, enabled, interval_seconds)

    def _on_card_loop_interval_changed(self, drone_id: str, interval_seconds: int) -> None:
        self._loop_intervals[drone_id] = interval_seconds
        self._persist_loop_state(drone_id, self._cards.get(drone_id, _DroneCard)._loop_toggle.isChecked() if drone_id in self._cards else False, interval_seconds)
        self.loopIntervalChanged.emit(drone_id, interval_seconds)

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.refresh()

    def is_open(self) -> bool:
        return self.isVisible()

    # -- Status polling -----------------------------------------------------

    def _detect_status(self, folder: Path) -> str:
        """Re-read drone.json and return a status string."""
        manifest = folder / "drone.json"
        if not manifest.exists():
            return "Needs Fix"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name"):
                return "Ready"
            return "Needs Fix"
        except (json.JSONDecodeError, OSError):
            return "Needs Fix"

    def _poll_status(self) -> None:
        """Periodic refresh of status badges by re-reading drone.json files."""
        if not self.isVisible():
            return
        for drone_id, card in list(self._cards.items()):
            folder = DroneStore.drone_folder(self._workspace_root, drone_id)
            status = self._detect_status(folder)
            card.update_status(status)

    # -- Geometry save/restore ----------------------------------------------

    def _restore_geometry(self, geometry: str) -> None:
        if not geometry:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        except Exception:
            logger.debug("Failed to restore Drone Workbay geometry", exc_info=True)

    def _schedule_geometry_save(self) -> None:
        if not self._geometry_restore_done:
            return
        self._geometry_save_timer.start()

    def _save_geometry(self) -> None:
        if not self._geometry_restore_done:
            return
        geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.geometry_saved.emit(geometry)

    # -- Events -------------------------------------------------------------

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._schedule_geometry_save()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._schedule_geometry_save()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._poll_timer.stop()
        event.accept()
