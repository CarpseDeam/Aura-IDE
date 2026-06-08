"""Floating Drone Reports window for live runs and saved receipts."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import BG, BG_ALT, BORDER, FG, FG_DIM, FG_MUTED


class DroneReportsWindow(QDialog):
    """Non-modal popout that owns Drone run cards.

    Hiding this window never cancels a run. Cards continue receiving updates
    through their existing Qt signal connections while the window is hidden.
    """

    visibility_changed = Signal(bool)
    geometry_saved = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        initial_geometry: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drone Reports")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self._geometry_restore_done = False
        self._initial_geometry = initial_geometry.strip()
        self._cards: dict[str, QWidget] = {}
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(250)
        self._geometry_save_timer.timeout.connect(self._save_geometry)
        self.resize(920, 640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("droneReportsHeader")
        header.setFixedHeight(44)
        header.setStyleSheet(
            f"QFrame#droneReportsHeader {{"
            f"  background: {BG};"
            f"  border-bottom: 1px solid {BORDER};"
            f"}}"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 0, 8, 0)
        header_layout.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(0)

        title = QLabel("Drone Reports", header)
        title.setStyleSheet(f"color: {FG}; font-weight: 700; font-size: 13px;")
        title_col.addWidget(title)

        subtitle = QLabel("Live run cards, tool output, and final receipts", header)
        subtitle.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        title_col.addWidget(subtitle)

        header_layout.addLayout(title_col)
        header_layout.addStretch(1)


        outer.addWidget(header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {BG_ALT}; border: none; }}"
            f"QScrollBar:vertical {{ background: {BG_ALT}; width: 10px; }}"
            f"QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; }}"
        )

        self._card_host = QWidget(self._scroll)
        self._card_host.setObjectName("droneReportsCardHost")
        self._card_host.setStyleSheet(f"background: {BG_ALT};")
        self._card_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._card_layout = QVBoxLayout(self._card_host)
        self._card_layout.setContentsMargins(14, 14, 14, 14)
        self._card_layout.setSpacing(12)

        self._empty_label = QLabel(
            "No Drone reports yet.\n\nLaunch a Drone from Drone Bay to see its run card here.",
            self._card_host,
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 13px; padding: 48px; "
            "background: transparent;"
        )
        self._card_layout.addWidget(self._empty_label)
        self._card_layout.addStretch(1)
        self._scroll.setWidget(self._card_host)
        outer.addWidget(self._scroll, 1)

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self.setStyleSheet(f"QDialog {{ background: {BG_ALT}; color: {FG}; }}")
        self._restore_geometry(self._initial_geometry)
        self._geometry_restore_done = True

    def add_run_card(self, run_id: str, card: QWidget) -> None:
        """Insert or replace one run/receipt card."""
        self.remove_run_card(run_id)
        self._cards[run_id] = card
        card.setParent(self._card_host)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._card_layout.insertWidget(self._card_layout.count() - 1, card)
        card.show()
        self._refresh_empty_state()

    def remove_run_card(self, run_id: str) -> None:
        card = self._cards.pop(run_id, None)
        if card is None:
            return
        self._card_layout.removeWidget(card)
        card.deleteLater()
        self._refresh_empty_state()

    def clear(self) -> None:
        for run_id in list(self._cards):
            self.remove_run_card(run_id)

    def has_card(self, run_id: str) -> bool:
        return run_id in self._cards

    def show_and_focus(self, run_id: str = "") -> None:
        self.show_and_raise()
        if run_id:
            QTimer.singleShot(0, lambda rid=run_id: self.focus_run_card(rid))

    def focus_run_card(self, run_id: str) -> None:
        card = self._cards.get(run_id)
        if card is None:
            return
        self._scroll.ensureWidgetVisible(card, 0, 28)
        card.setFocus(Qt.FocusReason.OtherFocusReason)
        if hasattr(card, "highlight_focus"):
            card.highlight_focus()

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_and_raise()

    def is_open(self) -> bool:
        return self.isVisible()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._schedule_geometry_save()
        self.visibility_changed.emit(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._schedule_geometry_save()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Prevent destruction — Drone Reports is a stable live monitor.

        Closing must never delete cards, clear runs, or touch runner state.
        The window stays open for the full lifecycle of active runs.
        """
        event.ignore()

    def _refresh_empty_state(self) -> None:
        self._empty_label.setVisible(not self._cards)

    def _restore_geometry(self, geometry: str) -> None:
        if not geometry:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        except Exception:
            return

    def _schedule_geometry_save(self) -> None:
        if not self._geometry_restore_done:
            return
        self._geometry_save_timer.start()

    def _save_geometry(self) -> None:
        if not self._geometry_restore_done:
            return
        geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.geometry_saved.emit(geometry)
