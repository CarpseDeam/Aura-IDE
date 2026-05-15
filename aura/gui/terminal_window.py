"""Floating terminal output window for run_terminal_command streams."""

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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards.terminal_card import TerminalCard
from aura.gui.theme import BG, BORDER, FG, FG_DIM, TERMINAL_BG


class TerminalWindow(QDialog):
    """Non-modal floating terminal output window.

    The window hides instead of being destroyed, so output continues buffering
    while hidden and reappears when the edge tab opens it again.
    """

    terminal_started = Signal()
    terminal_finished = Signal(int)
    visibility_changed = Signal(bool)
    terminal_cleared = Signal()
    geometry_saved = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        initial_geometry: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Terminal")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self._geometry_restore_done = False
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(250)
        self._geometry_save_timer.timeout.connect(self._save_geometry)
        self.resize(860, 460)

        self._current_tool_id: str | None = None
        self._terminal_card: TerminalCard | None = None
        self._last_exit_code: int | None = None
        self._initial_geometry = initial_geometry.strip()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("terminalWindowHeader")
        header.setFixedHeight(36)
        header.setStyleSheet(
            f"QFrame#terminalWindowHeader {{"
            f"  background: {BG};"
            f"  border-bottom: 1px solid {BORDER};"
            f"}}"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 8, 0)
        header_layout.setSpacing(8)

        title = QLabel("Terminal", header)
        title.setStyleSheet(f"color: {FG}; font-weight: 600;")
        header_layout.addWidget(title)
        header_layout.addStretch(1)

        close_btn = QToolButton(header)
        close_btn.setText("x")
        close_btn.setToolTip("Hide terminal")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QToolButton {{"
            f"  background: transparent;"
            f"  color: {FG_DIM};"
            f"  border: none;"
            f"  font-size: 14px;"
            f"  padding: 2px 8px;"
            f"}}"
            f"QToolButton:hover {{ color: {FG}; }}"
        )
        close_btn.clicked.connect(self.hide)
        header_layout.addWidget(close_btn)
        outer.addWidget(header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(f"background: {TERMINAL_BG}; border: none;")

        self._card_host = QWidget(self._scroll)
        self._card_host.setObjectName("terminalWindowCardHost")
        self._card_host.setStyleSheet(f"background: {TERMINAL_BG};")
        self._card_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._card_layout = QVBoxLayout(self._card_host)
        self._card_layout.setContentsMargins(10, 10, 10, 10)
        self._card_layout.setSpacing(0)
        self._card_layout.addStretch(1)
        self._scroll.setWidget(self._card_host)
        outer.addWidget(self._scroll, 1)

        self.setStyleSheet(
            f"QDialog {{ background: {TERMINAL_BG}; color: {FG}; }}"
        )
        self._restore_geometry(self._initial_geometry)
        self._geometry_restore_done = True

    def set_command(self, tool_id: str, command: str) -> None:
        """Replace current terminal card for a new command."""
        self._current_tool_id = tool_id
        self._last_exit_code = None
        self._remove_card()

        card = TerminalCard(
            command=command,
            parent=self._card_host,
            start_collapsed=False,
        )
        self._terminal_card = card
        self._card_layout.insertWidget(0, card)
        self.terminal_started.emit()

    def append_output(self, tool_id: str, text: str) -> None:
        """Forward output text to the active card, even while hidden."""
        if tool_id != self._current_tool_id:
            return
        if self._terminal_card is not None:
            self._terminal_card.append_output(text)

    def set_result(self, tool_id: str, exit_code: int) -> None:
        """Finalize terminal state; failed commands raise the window."""
        if tool_id != self._current_tool_id:
            return
        self._last_exit_code = exit_code

        if self._terminal_card is not None:
            self._terminal_card.set_result(exit_code)
            if self.isVisible():
                self._terminal_card.expand()

        if exit_code != 0:
            self.show_and_raise()

        self.terminal_finished.emit(exit_code)

    def show_and_raise(self) -> None:
        """Show this floating window and bring it to the front."""
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle(self) -> None:
        """Toggle between visible and hidden."""
        if self.isVisible():
            self.hide()
        else:
            self.show_and_raise()

    def is_open(self) -> bool:
        """Return whether the floating terminal window is visible."""
        return self.isVisible()

    def clear(self) -> None:
        """Delete the current terminal card and reset state."""
        self._current_tool_id = None
        self._last_exit_code = None
        self._remove_card()
        self.terminal_cleared.emit()

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
        event.ignore()
        self._save_geometry()
        self.hide()

    def _remove_card(self) -> None:
        if self._terminal_card is None:
            return
        self._card_layout.removeWidget(self._terminal_card)
        self._terminal_card.deleteLater()
        self._terminal_card = None

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
        geometry = bytes(
            self.saveGeometry().toBase64()
        ).decode("ascii")
        self.geometry_saved.emit(geometry)
