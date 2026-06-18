"""Modeless Drone Workbay window — card menu of saved Drones."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.drones.definition import DroneDefinition
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

        # Title row: status dot + name
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        dot_color = STATUS_COLORS.get(status, FG_MUTED)
        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        self._dot.setStyleSheet(
            f"background: {dot_color};"
            f"border-radius: 5px;"
            f"border: none;"
        )
        title_row.addWidget(self._dot)

        title = QLabel(name)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPixelSize(13)
        title.setFont(title_font)
        title.setStyleSheet(
            f"color: {_qss_color(FG)}; background: transparent; border: none;"
        )
        title_row.addWidget(title, 1)
        layout.addLayout(title_row)

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

        # Description (max 2 lines)
        self._desc = QLabel(description if description else "")
        self._desc.setWordWrap(True)
        self._desc.setMaximumHeight(36)
        self._desc.setStyleSheet(
            f"color: {_qss_color(FG_MUTED)}; font-size: 11px; background: transparent; border: none;"
        )
        self._desc.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._desc)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

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
        btn_row.addWidget(self._btn_run)

        # Delete
        self._btn_delete = QPushButton("\u2715")
        self._btn_delete.setFixedWidth(28)
        self._btn_delete.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid transparent;"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG_DIM)};"
            f"  font-size: 12px;"
            f"  padding: 3px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  color: {_qss_color(DANGER)};"
            f"  border-color: rgba(248, 113, 113, 0.20);"
            f"}}"
        )
        self._btn_delete.setCursor(Qt.PointingHandCursor)
        btn_row.addWidget(self._btn_delete)

        # Loop toggle
        self._btn_loop = QPushButton()
        self._set_loop_button_state(loop_enabled)
        self._btn_loop.setCursor(Qt.PointingHandCursor)
        btn_row.addWidget(self._btn_loop)

        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _set_loop_button_state(self, enabled: bool) -> None:
        self._loop_enabled = enabled
        if enabled:
            self._btn_loop.setText("\U0001f504 Looping")
            self._btn_loop.setStyleSheet(
                f"QPushButton {{"
                f"  background: rgba(158, 206, 106, 0.15);"
                f"  border: 1px solid rgba(158, 206, 106, 0.30);"
                f"  border-radius: 4px;"
                f"  color: {_qss_color(SUCCESS)};"
                f"  font-size: 10px;"
                f"  padding: 3px 10px;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background: rgba(158, 206, 106, 0.25);"
                f"}}"
            )
        else:
            self._btn_loop.setText("\U0001f501 Loop")
            self._btn_loop.setStyleSheet(
                f"QPushButton {{"
                f"  background: transparent;"
                f"  border: 1px solid rgba(255, 255, 255, 0.06);"
                f"  border-radius: 4px;"
                f"  color: {_qss_color(FG_DIM)};"
                f"  font-size: 10px;"
                f"  padding: 3px 10px;"
                f"}}"
                f"QPushButton:hover {{"
                f"  border-color: rgba(255, 255, 255, 0.15);"
                f"  color: {_qss_color(FG)};"
                f"}}"
            )

    @property
    def loop_enabled(self) -> bool:
        return self._loop_enabled

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


class DroneWorkbayWindow(QDialog):
    """Non-modal window showing a scrollable card menu of saved Drones.

    Hiding this window preserves the card list state. WA_DeleteOnClose
    is False so closing via the WM close button only hides the window.
    """

    geometry_saved = Signal(str)

    runDroneRequested = Signal(str, str)  # drone_id, folder
    deleteDroneRequested = Signal(str)    # drone_id
    loopDroneRequested = Signal(str, bool)  # drone_id, enabled

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

        self._restore_geometry(self._initial_geometry)
        self._geometry_restore_done = True

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
                )
                card._btn_run.clicked.connect(
                    lambda checked, did=d.id: self.runDroneRequested.emit(did, "")
                )
                card._btn_delete.clicked.connect(
                    lambda checked, did=d.id: self.deleteDroneRequested.emit(did)
                )
                # Loop button wiring (must capture current loop state)
                card._btn_loop.clicked.connect(
                    lambda checked, did=d.id, c=card: self._on_card_loop_toggled(did, c)
                )
                self._cards[d.id] = card
                self._card_layout.insertWidget(self._card_layout.count() - 1, card)

    def set_card_loop_state(self, drone_id: str, enabled: bool) -> None:
        """Update the loop button state on a drone card."""
        card = self._cards.get(drone_id)
        if card is not None:
            card._set_loop_button_state(enabled)

    def _on_card_loop_toggled(self, drone_id: str, card: _DroneCard) -> None:
        enabled = not card.loop_enabled
        card._set_loop_button_state(enabled)
        self.loopDroneRequested.emit(drone_id, enabled)

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
