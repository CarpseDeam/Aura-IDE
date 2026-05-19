"""Edge tab rail — collapsible sidebar with terminal and preview tabs."""
from __future__ import annotations

from enum import Enum, auto

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout, QWidget

from aura.config import media_path
from aura.gui.theme import ACCENT, BG_RAISED, BORDER, DANGER, FG, FG_DIM, SUCCESS, WARN


class TerminalTabState(Enum):
    EXPANDED = auto()
    COLLAPSED = auto()
    HIDDEN = auto()


class EdgeTabRail(QFrame):
    """Vertical tab rail on the edge of the workspace, hosting
    a terminal tab with expand/collapse/hide states and a checkpoint tab."""

    terminalTabToggled = Signal(bool)  # True=expanded, False=collapsed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: str = "dim"
        self._is_terminal_open: bool = False
        self._terminal_tab: QToolButton | None = None
        self._checkpoint_tab: QToolButton | None = None
        self._terminal_container: QWidget | None = None
        self._corner_widget: QWidget | None = None
        self._setup_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setObjectName("edgeTabRail")
        self.setFixedWidth(40)
        self.setStyleSheet(
            "QFrame#edgeTabRail { background: transparent; border: none; }"
        )

        rail_layout = QVBoxLayout(self)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(6)

        self._terminal_tab = QToolButton(self)
        self._terminal_tab.setObjectName("edgeTerminalTab")
        self._terminal_tab.setText("$")
        self._terminal_tab.setToolTip("Toggle terminal output")
        self._terminal_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._terminal_tab.setCheckable(True)
        self._terminal_tab.setFixedSize(40, 44)
        self._terminal_tab.clicked.connect(lambda: self.terminalTabToggled.emit(self._terminal_tab.isChecked()))
        rail_layout.addWidget(self._terminal_tab)

        self._checkpoint_tab = QToolButton(self)
        self._checkpoint_tab.setObjectName("edgeCheckpointTab")
        self._checkpoint_tab.setToolTip("Checkpoint Timeline")
        self._checkpoint_tab.setIcon(QIcon(str(media_path("account_tree_.svg"))))
        self._checkpoint_tab.setIconSize(QSize(22, 22))
        self._checkpoint_tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._checkpoint_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checkpoint_tab.setFixedSize(40, 44)
        self._checkpoint_tab.setStyleSheet(self._checkpoint_tab_style())
        rail_layout.addWidget(self._checkpoint_tab)

        self.adjustSize()
        self.set_state("dim")
        self.raise_()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        self._state = state
        if self._terminal_tab is not None:
            self._terminal_tab.setStyleSheet(self._terminal_tab_style(state))

    @property
    def terminal_tab(self) -> QToolButton | None:
        return self._terminal_tab

    @property
    def checkpoint_tab(self) -> QToolButton | None:
        return self._checkpoint_tab

    @property
    def terminal_container(self) -> QWidget | None:
        return self._terminal_container

    @property
    def corner_widget(self) -> QWidget | None:
        return self._corner_widget

    def set_is_terminal_open(self, is_open: bool) -> None:
        """Notify the rail whether the terminal window is open, so the
        'dim' state can show the active variant."""
        self._is_terminal_open = is_open

    # ------------------------------------------------------------------
    # Stylesheets
    # ------------------------------------------------------------------

    def _terminal_tab_style(self, state: str) -> str:
        palette = {
            "dim": (BG_RAISED, FG_DIM, BORDER),
            "running": ("#3a2d16", WARN, WARN),
            "success": ("#17351d", SUCCESS, SUCCESS),
            "failure": ("#3a151b", DANGER, DANGER),
        }
        bg, fg, border = palette.get(state, palette["dim"])
        if state == "dim" and self._is_terminal_open:
            bg, fg, border = ("#18243a", FG, ACCENT)

        return (
            "QToolButton#edgeTerminalTab {"
            f"  background: {bg};"
            f"  color: {fg};"
            f"  border: 1px solid {border};"
            "  border-right: none;"
            "  border-top-left-radius: 8px;"
            "  border-bottom-left-radius: 8px;"
            "  border-top-right-radius: 0px;"
            "  border-bottom-right-radius: 0px;"
            "  font-size: 18px;"
            "  font-weight: 700;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeTerminalTab:hover {"
            "  background: #2b2b34;"
            f"  color: {FG};"
            f"  border-color: {ACCENT};"
            "  border-right: none;"
            "}"
        )

    def _checkpoint_tab_style(self) -> str:
        neon = "#39ff88"
        return (
            "QToolButton#edgeCheckpointTab {"
            "  background: #0b2514;"
            f"  color: {neon};"
            f"  border: 1px solid {neon};"
            "  border-right: none;"
            "  border-top-left-radius: 8px;"
            "  border-bottom-left-radius: 8px;"
            "  border-top-right-radius: 0px;"
            "  border-bottom-right-radius: 0px;"
            "  font-size: 18px;"
            "  font-weight: 800;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeCheckpointTab:hover {"
            "  background: #123d22;"
            f"  color: {FG};"
            "}"
        )
