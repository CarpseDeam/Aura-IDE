"""Edge tab rail — collapsible sidebar with terminal and preview tabs."""
from __future__ import annotations

from enum import Enum, auto

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout, QWidget

from aura.config import media_path
from aura.gui.theme import (
    ACCENT,
    ACCENT_HOVER,
    BG_RAISED,
    BORDER,
    DANGER,
    FG,
    FG_DIM,
    LABEL_FILES,
    LABEL_PROJECTS,
    SUCCESS,
    WARN,
)


class TerminalTabState(Enum):
    EXPANDED = auto()
    COLLAPSED = auto()
    HIDDEN = auto()


class EdgeTabRail(QFrame):
    """Vertical tab rail on the edge of the workspace, hosting
    a terminal tab with expand/collapse/hide states and a checkpoint tab."""

    terminalTabToggled = Signal(bool)  # True=expanded, False=collapsed
    agentsRequested = Signal()
    companionRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: str = "dim"
        self._is_terminal_open: bool = False
        self._terminal_tab: QToolButton | None = None
        self._checkpoint_tab: QToolButton | None = None
        self._terminal_container: QWidget | None = None
        self._agents_tab: QToolButton | None = None
        self._companion_tab: QToolButton | None = None
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

        self._rail_layout = QVBoxLayout(self)
        self._rail_layout.setContentsMargins(0, 0, 0, 0)
        self._rail_layout.setSpacing(6)

        self._rail_layout.addStretch(1)
        self._rail_layout.addSpacing(2)

        self._terminal_tab = QToolButton(self)
        self._terminal_tab.setObjectName("edgeTerminalTab")
        self._terminal_tab.setIcon(QIcon(str(media_path("terminal_2_24dp.svg"))))
        self._terminal_tab.setIconSize(QSize(22, 22))
        self._terminal_tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._terminal_tab.setToolTip("Toggle terminal output")
        self._terminal_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._terminal_tab.setCheckable(True)
        self._terminal_tab.setFixedSize(40, 44)
        self._terminal_tab.clicked.connect(lambda: self.terminalTabToggled.emit(self._terminal_tab.isChecked()))
        self._rail_layout.addWidget(self._terminal_tab)

        self._checkpoint_tab = QToolButton(self)
        self._checkpoint_tab.setObjectName("edgeCheckpointTab")
        self._checkpoint_tab.setToolTip("Checkpoint Timeline")
        self._checkpoint_tab.setIcon(QIcon(str(media_path("account_tree_.svg"))))
        self._checkpoint_tab.setIconSize(QSize(22, 22))
        self._checkpoint_tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._checkpoint_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checkpoint_tab.setFixedSize(40, 44)
        self._checkpoint_tab.setStyleSheet(self._checkpoint_tab_style())
        self._rail_layout.addWidget(self._checkpoint_tab)

        self._agents_tab = QToolButton(self)
        self._agents_tab.setObjectName("edgeAgentsTab")
        self._agents_tab.setToolTip("Agents")
        self._agents_tab.setIcon(QIcon(str(media_path("agents_bot.svg"))))
        self._agents_tab.setIconSize(QSize(22, 22))
        self._agents_tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._agents_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._agents_tab.setCheckable(True)
        self._agents_tab.setFixedSize(40, 44)
        self._agents_tab.clicked.connect(lambda: self.agentsRequested.emit())
        self._agents_tab.setStyleSheet(self._agents_tab_style())
        self._rail_layout.addWidget(self._agents_tab)

        # Companion phone badge
        self._companion_tab = QToolButton(self)
        self._companion_tab.setObjectName("edgeCompanionTab")
        self._companion_tab.setToolTip("Aura Companion")
        self._companion_tab.setIcon(QIcon(str(media_path("phone_24.svg"))))
        self._companion_tab.setIconSize(QSize(22, 22))
        self._companion_tab.setFixedSize(40, 44)
        self._companion_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._companion_tab.clicked.connect(lambda: self.companionRequested.emit())
        self._companion_tab.setStyleSheet(self._companion_tab_style())
        self._rail_layout.addWidget(self._companion_tab)

        self.set_state("dim")

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
    def agents_tab(self) -> QToolButton | None:
        return self._agents_tab

    @property
    def companion_tab(self) -> QToolButton | None:
        return self._companion_tab

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

    def set_companion_status(self, status: str, error: str = "") -> None:
        tab = self._companion_tab
        if tab is None:
            return
        if status == "connected":
            tab.setStyleSheet(self._companion_tab_style(accent="#39ff88", bg="#0b2514"))
            tab.setToolTip("Companion — Connected")
        elif status in ("connecting", "starting_local_relay"):
            tab.setStyleSheet(self._companion_tab_style(accent="#7dcfff", bg="#14303a"))
            tab.setToolTip("Companion — Connecting…")
        elif status == "error" or status == "disconnected":
            tab.setStyleSheet(self._companion_tab_style(accent=DANGER, bg="#1a1111"))
            tab.setToolTip(f"Companion — {error or 'Connection error'}")
        else:  # "disabled" or other
            tab.setStyleSheet(self._companion_tab_style(accent=FG_DIM, bg="#161b33"))
            tab.setToolTip("Companion — Disabled")

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
            "  border-left: none;"
            "  border-top-left-radius: 0px;"
            "  border-bottom-left-radius: 0px;"
            "  border-top-right-radius: 8px;"
            "  border-bottom-right-radius: 8px;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeTerminalTab:hover {"
            "  background: #2b2b34;"
            f"  color: {FG};"
            f"  border-color: {ACCENT};"
            "  border-left: none;"
            "}"
        )

    def _agents_tab_style(self) -> str:
        return (
            "QToolButton#edgeAgentsTab {"
            "  background: #161b33;"
            f"  color: {LABEL_FILES};"
            f"  border: 1px solid {ACCENT};"
            "  border-left: none;"
            "  border-top-left-radius: 0px;"
            "  border-bottom-left-radius: 0px;"
            "  border-top-right-radius: 8px;"
            "  border-bottom-right-radius: 8px;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeAgentsTab:hover {"
            "  background: #1d2f55;"
            f"  color: {ACCENT_HOVER};"
            f"  border-color: {LABEL_FILES};"
            "  border-left: none;"
            "}"
            "QToolButton#edgeAgentsTab:checked {"
            "  background: #221b44;"
            f"  color: {LABEL_PROJECTS};"
            f"  border-color: {LABEL_PROJECTS};"
            "  border-left: none;"
            "}"
            "QToolButton#edgeAgentsTab:checked:hover {"
            "  background: #2a245f;"
            f"  color: {FG};"
            f"  border-color: {ACCENT_HOVER};"
            "  border-left: none;"
            "}"
        )

    def _checkpoint_tab_style(self) -> str:
        neon = "#39ff88"
        return (
            "QToolButton#edgeCheckpointTab {"
            "  background: #0b2514;"
            f"  color: {neon};"
            f"  border: 1px solid {neon};"
            "  border-left: none;"
            "  border-top-left-radius: 0px;"
            "  border-bottom-left-radius: 0px;"
            "  border-top-right-radius: 8px;"
            "  border-bottom-right-radius: 8px;"
            "  font-size: 18px;"
            "  font-weight: 800;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeCheckpointTab:hover {"
            "  background: #123d22;"
            f"  color: {FG};"
            "}"
        )

    def _companion_tab_style(self, accent: str = FG_DIM, bg: str = "#161b33") -> str:
        return (
            "QToolButton#edgeCompanionTab {"
            f"  background: {bg};"
            f"  color: {accent};"
            f"  border: 1px solid {accent};"
            "  border-left: none;"
            "  border-top-left-radius: 0px;"
            "  border-bottom-left-radius: 0px;"
            "  border-top-right-radius: 8px;"
            "  border-bottom-right-radius: 8px;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeCompanionTab:hover {"
            f"  background: #1d2f55;"
            f"  color: {ACCENT_HOVER};"
            f"  border-color: {ACCENT_HOVER};"
            "  border-left: none;"
            "}"
        )
