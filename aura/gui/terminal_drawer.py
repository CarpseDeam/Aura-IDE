"""Terminal drawer panel — shows streaming terminal output from run_terminal_command."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards.terminal_card import TerminalCard
from aura.gui.theme import BG, BORDER, FG_DIM, TERMINAL_BG


class TerminalDrawer(QWidget):
    """Sliding drawer for terminal output logs.

    Emits terminal_started when a new command begins and terminal_finished(exit_code)
    when a command completes.

    Layout (top-to-bottom):
      1. Drawer panel (QFrame, initially hidden) — contains a header bar with
         close button + a TerminalCard for streaming output.

    Public API:
        set_command(tool_id, command) -> None
        append_output(tool_id, text) -> None
        set_result(tool_id, exit_code) -> None
        open() -> None
        close() -> None
        toggle() -> None
        clear() -> None
    """

    terminal_started = Signal()
    terminal_finished = Signal(int)  # exit_code: 0=success, nonzero=failure
    visibility_changed = Signal(bool)
    terminal_cleared = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Internal state
        self._is_open: bool = False
        self._current_tool_id: str | None = None
        self._terminal_card: TerminalCard | None = None
        self._last_exit_code: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Drawer panel (hidden by default) ---
        self._drawer = QFrame(self)
        self._drawer.setObjectName("terminalDrawerPanel")
        self._drawer.setMinimumSize(0, 0)
        self._drawer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._drawer.setStyleSheet(
            f"QFrame#terminalDrawerPanel {{"
            f"  background: {TERMINAL_BG};"
            f"  border-top: 1px solid {BORDER};"
            f"}}"
        )
        self._drawer.setVisible(False)

        drawer_layout = QVBoxLayout(self._drawer)
        drawer_layout.setContentsMargins(0, 0, 0, 0)
        drawer_layout.setSpacing(0)

        # Drawer header bar
        drawer_header = QFrame(self._drawer)
        drawer_header.setObjectName("drawerHeader")
        drawer_header.setFixedHeight(32)
        drawer_header.setStyleSheet(
            f"QFrame#drawerHeader {{"
            f"  background: {BG};"
            f"  border-bottom: 1px solid {BORDER};"
            f"}}"
        )
        header_layout = QHBoxLayout(drawer_header)
        header_layout.setContentsMargins(12, 0, 8, 0)
        header_layout.setSpacing(8)

        header_layout.addStretch(1)

        self._close_btn = QToolButton(drawer_header)
        self._close_btn.setText("✕")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            f"QToolButton {{"
            f"  background: transparent;"
            f"  color: {FG_DIM};"
            f"  border: none;"
            f"  font-size: 14px;"
            f"  padding: 2px 8px;"
            f"}}"
            f"QToolButton:hover {{"
            f"  color: #ffffff;"
            f"}}"
        )
        self._close_btn.clicked.connect(self.close)
        header_layout.addWidget(self._close_btn)

        drawer_layout.addWidget(drawer_header)

        # Terminal card area — a container that holds the TerminalCard
        self._card_container = QWidget(self._drawer)
        self._card_container.setObjectName("cardContainer")
        self._card_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        card_container_layout = QVBoxLayout(self._card_container)
        card_container_layout.setContentsMargins(0, 0, 0, 0)
        card_container_layout.setSpacing(0)
        drawer_layout.addWidget(self._card_container, 1)

        layout.addWidget(self._drawer)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_command(self, tool_id: str, command: str) -> None:
        """Replace current TerminalCard with a new one for *tool_id*.

        Clears the last exit code and emits terminal_started.
        """
        self._current_tool_id = tool_id
        self._last_exit_code = None

        # Remove existing terminal card
        self._remove_card()

        # Create new card (start expanded so the user sees the command)
        card = TerminalCard(command=command, parent=self._card_container, start_collapsed=False)
        self._terminal_card = card
        # Remove layout margins from card container so the card fills its space
        container_layout = self._card_container.layout()
        container_layout.addWidget(card)

        self.terminal_started.emit()

    def append_output(self, tool_id: str, text: str) -> None:
        """Forward output text to the current TerminalCard if *tool_id* matches."""
        if tool_id != self._current_tool_id:
            return
        if self._terminal_card is not None:
            self._terminal_card.append_output(text)

    def set_result(self, tool_id: str, exit_code: int) -> None:
        """Set the result (exit code) for the terminal session.

        Auto-opens the drawer on failure and emits terminal_finished(exit_code).
        """
        if tool_id != self._current_tool_id:
            return
        self._last_exit_code = exit_code

        if self._terminal_card is not None:
            self._terminal_card.set_result(exit_code)
            # If drawer is open, keep card expanded (counteract auto-collapse on success)
            if self._is_open:
                self._terminal_card.expand()

        # Auto-open drawer on failure
        if exit_code != 0:
            self.open()

        self.terminal_finished.emit(exit_code)

    def open(self) -> None:
        """Show the drawer panel and expand the TerminalCard."""
        was_open = self._is_open
        self._is_open = True
        self._drawer.setVisible(True)
        if self._terminal_card is not None:
            self._terminal_card.expand()
        if not was_open:
            self.visibility_changed.emit(True)

    def close(self) -> None:
        """Hide the drawer panel and collapse the TerminalCard."""
        was_open = self._is_open
        self._is_open = False
        self._drawer.setVisible(False)
        if self._terminal_card is not None:
            self._terminal_card.collapse()
        if was_open:
            self.visibility_changed.emit(False)

    def toggle(self) -> None:
        """Toggle between open and closed states."""
        if self._is_open:
            self.close()
        else:
            self.open()

    def is_open(self) -> bool:
        """Return whether the drawer panel is currently visible."""
        return self._is_open

    def clear(self) -> None:
        """Delete the TerminalCard, reset all state, and hide panel."""
        was_open = self._is_open
        self._current_tool_id = None
        self._last_exit_code = None
        self._remove_card()
        self._drawer.setVisible(False)
        self._is_open = False
        if was_open:
            self.visibility_changed.emit(False)
        self.terminal_cleared.emit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _remove_card(self) -> None:
        """Remove and delete the current TerminalCard from the container."""
        if self._terminal_card is not None:
            layout = self._card_container.layout()
            layout.removeWidget(self._terminal_card)
            self._terminal_card.deleteLater()
            self._terminal_card = None
