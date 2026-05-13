"""Independent bottom-right Terminal drawer for terminal output logs.

The TerminalDrawer sits below the workspace splitter and provides a "$" launcher
button that toggles a dark drawer panel showing the current terminal session's
output. It replaces the terminal tab behaviour that previously lived inside
InfoHubPane.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards.terminal_card import TerminalCard
from aura.gui.theme import BG, BORDER, DANGER, FG_DIM, SUCCESS, TERMINAL_BG


class TerminalDrawer(QWidget):
    """Bottom-right sliding drawer for terminal output logs.

    Layout (top-to-bottom):
      1. Drawer panel (QFrame, initially hidden) — contains a header bar with
         close button + a TerminalCard for streaming output.
      2. Launcher bar (QFrame, always visible, 28px height) — right-aligned "$"
         QToolButton.

    Public API:
        set_command(tool_id, command) -> None
        append_output(tool_id, text) -> None
        set_result(tool_id, exit_code) -> None
        open() -> None
        close() -> None
        toggle() -> None
        clear() -> None
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Internal state
        self._is_open: bool = False
        self._current_tool_id: str | None = None
        self._terminal_card: TerminalCard | None = None
        self._last_exit_code: int | None = None

        # Timer for resetting launcher colour after success flash
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(2000)
        self._state_timer.timeout.connect(self._reset_launcher_after_success)

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

        # --- Launcher bar (always visible) ---
        self._launcher_bar = QFrame(self)
        self._launcher_bar.setObjectName("terminalLauncherBar")
        self._launcher_bar.setFixedHeight(28)
        self._launcher_bar.setStyleSheet(
            f"QFrame#terminalLauncherBar {{"
            f"  background: {BG};"
            f"  border-top: 1px solid {BORDER};"
            f"}}"
        )

        launcher_layout = QHBoxLayout(self._launcher_bar)
        launcher_layout.setContentsMargins(0, 0, 8, 0)
        launcher_layout.setSpacing(0)
        launcher_layout.addStretch(1)

        self._launcher_btn = QToolButton(self._launcher_bar)
        self._launcher_btn.setText("$")
        self._launcher_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._launcher_btn.setToolTip("Toggle terminal drawer")
        self._launcher_btn.setStyleSheet(
            f"QToolButton {{"
            f"  background: transparent;"
            f"  color: {FG_DIM};"
            f"  border: none;"
            f"  font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
            f"  font-size: 14px;"
            f"  font-weight: 600;"
            f"  padding: 2px 8px;"
            f"}}"
            f"QToolButton:hover {{"
            f"  color: #ffffff;"
            f"}}"
        )
        self._launcher_btn.clicked.connect(self.toggle)
        launcher_layout.addWidget(self._launcher_btn)

        layout.addWidget(self._launcher_bar)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_command(self, tool_id: str, command: str) -> None:
        """Replace current TerminalCard with a new one for *tool_id*.

        Resets the launcher colour to FG_DIM and clears the last exit code.
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

        # Reset launcher colour
        self._launcher_btn.setStyleSheet(
            f"QToolButton {{"
            f"  background: transparent;"
            f"  color: {FG_DIM};"
            f"  border: none;"
            f"  font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
            f"  font-size: 14px;"
            f"  font-weight: 600;"
            f"  padding: 2px 8px;"
            f"}}"
            f"QToolButton:hover {{"
            f"  color: #ffffff;"
            f"}}"
        )

    def append_output(self, tool_id: str, text: str) -> None:
        """Forward output text to the current TerminalCard if *tool_id* matches."""
        if tool_id != self._current_tool_id:
            return
        if self._terminal_card is not None:
            self._terminal_card.append_output(text)

    def set_result(self, tool_id: str, exit_code: int) -> None:
        """Set the result (exit code) for the terminal session.

        Updates the launcher colour: green flash on success (2s), red persistent
        on failure. Auto-opens the drawer on failure.
        """
        if tool_id != self._current_tool_id:
            return
        self._last_exit_code = exit_code

        if self._terminal_card is not None:
            self._terminal_card.set_result(exit_code)

        if exit_code == 0:
            # Success: flash green, then reset after 2s
            self._launcher_btn.setStyleSheet(
                f"QToolButton {{"
                f"  background: transparent;"
                f"  color: {SUCCESS};"
                f"  border: none;"
                f"  font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
                f"  font-size: 14px;"
                f"  font-weight: 600;"
                f"  padding: 2px 8px;"
                f"}}"
                f"QToolButton:hover {{"
                f"  color: #ffffff;"
                f"}}"
            )
            self._state_timer.start()
        else:
            # Failure: red persistent, auto-open drawer
            self._launcher_btn.setStyleSheet(
                f"QToolButton {{"
                f"  background: transparent;"
                f"  color: {DANGER};"
                f"  border: none;"
                f"  font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
                f"  font-size: 14px;"
                f"  font-weight: 600;"
                f"  padding: 2px 8px;"
                f"}}"
                f"QToolButton:hover {{"
                f"  color: #ffffff;"
                f"}}"
            )
            self.open()

    def open(self) -> None:
        """Show the drawer panel, expand the TerminalCard, and update launcher."""
        self._is_open = True
        self._drawer.setVisible(True)
        if self._terminal_card is not None:
            self._terminal_card.expand()
        self._update_launcher_state()

    def close(self) -> None:
        """Hide the drawer panel and restore launcher colour based on last state."""
        self._is_open = False
        self._drawer.setVisible(False)
        if self._terminal_card is not None:
            self._terminal_card.collapse()
        self._update_launcher_state()

    def toggle(self) -> None:
        """Toggle between open and closed states."""
        if self._is_open:
            self.close()
        else:
            self.open()

    def clear(self) -> None:
        """Delete the TerminalCard, reset all state, hide panel, reset launcher."""
        self._current_tool_id = None
        self._last_exit_code = None
        self._state_timer.stop()
        self._remove_card()
        self._drawer.setVisible(False)
        self._is_open = False
        self._launcher_btn.setStyleSheet(
            f"QToolButton {{"
            f"  background: transparent;"
            f"  color: {FG_DIM};"
            f"  border: none;"
            f"  font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
            f"  font-size: 14px;"
            f"  font-weight: 600;"
            f"  padding: 2px 8px;"
            f"}}"
            f"QToolButton:hover {{"
            f"  color: #ffffff;"
            f"}}"
        )

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

    def _update_launcher_state(self) -> None:
        """Update launcher colour based on drawer open state and last exit code.

        When open: brighter.
        When closed: restore to last state (success green, failure red, or default dim).
        """
        if self._last_exit_code is not None and self._last_exit_code != 0:
            # Persistent failure red
            color = DANGER
        elif self._last_exit_code == 0 and not self._is_open:
            # Success — dim again
            color = FG_DIM
        else:
            color = FG_DIM

        self._launcher_btn.setStyleSheet(
            f"QToolButton {{"
            f"  background: transparent;"
            f"  color: {color};"
            f"  border: none;"
            f"  font-family: 'Geist Mono', 'JetBrains Mono', monospace;"
            f"  font-size: 14px;"
            f"  font-weight: 600;"
            f"  padding: 2px 8px;"
            f"}}"
            f"QToolButton:hover {{"
            f"  color: #ffffff;"
            f"}}"
        )

    def _reset_launcher_after_success(self) -> None:
        """Reset launcher colour to FG_DIM after the success flash timer expires."""
        self._update_launcher_state()
