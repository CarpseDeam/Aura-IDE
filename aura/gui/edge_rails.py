"""Edge tab rail — collapsible sidebar with terminal and preview tabs."""
from __future__ import annotations

from enum import Enum, auto

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout, QWidget

from aura.config import media_path
from aura.gui.drones.drone_rail_pip import DroneRailPip
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
    droneBayRequested = Signal()
    droneRunFocusRequested = Signal(str)
    companionRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: str = "dim"
        self._is_terminal_open: bool = False
        self._terminal_tab: QToolButton | None = None
        self._checkpoint_tab: QToolButton | None = None
        self._terminal_container: QWidget | None = None
        self._drone_tab: QToolButton | None = None
        self._companion_tab: QToolButton | None = None
        self._drone_run_pips: dict[str, DroneRailPip] = {}
        self._summon_animations: dict[str, tuple[QToolButton, QPropertyAnimation]] = {}
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

        self._drone_run_stack = QWidget(self)
        self._drone_run_stack.setObjectName("droneRunStack")
        self._drone_run_stack_layout = QVBoxLayout(self._drone_run_stack)
        self._drone_run_stack_layout.setContentsMargins(0, 0, 0, 0)
        self._drone_run_stack_layout.setSpacing(4)
        self._drone_run_stack_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)

        self._rail_layout.addStretch(1)
        self._rail_layout.addWidget(self._drone_run_stack, alignment=Qt.AlignmentFlag.AlignCenter)
        self._rail_layout.addSpacing(2)

        self._terminal_tab = QToolButton(self)
        self._terminal_tab.setObjectName("edgeTerminalTab")
        self._terminal_tab.setText("$")
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

        self._drone_tab = QToolButton(self)
        self._drone_tab.setObjectName("edgeDroneTab")
        self._drone_tab.setToolTip("Drone Bay")
        self._drone_tab.setIcon(QIcon(str(media_path("drone_bot.svg"))))
        self._drone_tab.setIconSize(QSize(22, 22))
        self._drone_tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._drone_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drone_tab.setCheckable(True)
        self._drone_tab.setFixedSize(40, 44)
        self._drone_tab.clicked.connect(lambda: self.droneBayRequested.emit())
        self._drone_tab.setStyleSheet(self._drone_tab_style())
        self._rail_layout.addWidget(self._drone_tab)

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
    def drone_tab(self) -> QToolButton | None:
        return self._drone_tab

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

    def add_drone_run_pip(self, run_id: str, drone_name: str) -> None:
        if run_id in self._drone_run_pips:
            return
        pip = DroneRailPip(self)
        pip.setToolTip(f"{drone_name}: summoning")
        pip.focused.connect(lambda rid=run_id: self.droneRunFocusRequested.emit(rid))
        pip.set_summoning()
        pip.begin_launch_animation()
        self._drone_run_pips[run_id] = pip
        self._drone_run_stack_layout.addWidget(pip, alignment=Qt.AlignmentFlag.AlignCenter)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())
        QTimer.singleShot(0, lambda rid=run_id: self._play_summon_animation(rid))

    def set_drone_run_pip_state(self, run_id: str, drone_name: str, state: str) -> None:
        pip = self._drone_run_pips.get(run_id)
        if pip is None:
            return
        normalized = self._normalize_drone_state(state)
        label = normalized.replace("_", " ")
        pip.setToolTip(f"{drone_name}: {label}")
        if normalized == "summoning":
            pip.set_summoning()
        elif normalized == "completed":
            pip.set_completed()
        elif normalized in {"failed", "timed_out"}:
            pip.set_error()
        elif normalized == "cancelled":
            pip.set_cancelled()
        elif normalized == "waiting_for_approval":
            pip.set_waiting_for_approval()
        elif normalized == "running":
            pip.set_running()
        elif normalized == "waiting_for_loop":
            pip.set_looping_idle()

    def remove_drone_run_pip(self, run_id: str) -> None:
        ghost, animation = self._summon_animations.pop(run_id, (None, None))
        if animation is not None:
            animation.stop()
        if ghost is not None:
            ghost.deleteLater()
        pip = self._drone_run_pips.pop(run_id, None)
        if pip is None:
            return
        self._drone_run_stack_layout.removeWidget(pip)
        pip.deleteLater()
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    def rekey_drone_run_pip(self, old_run_id: str, new_run_id: str) -> None:
        """Move a pip widget from old_run_id to new_run_id without visual disruption."""
        pip = self._drone_run_pips.pop(old_run_id, None)
        if pip is not None:
            try:
                pip.focused.disconnect()
            except RuntimeError:
                pass
            pip.focused.connect(lambda rid=new_run_id: self.droneRunFocusRequested.emit(rid))
            self._drone_run_pips[new_run_id] = pip

    def _play_summon_animation(self, run_id: str) -> None:
        pip = self._drone_run_pips.get(run_id)
        if pip is None:
            return
        if self._drone_tab is None:
            pip.finish_launch_animation()
            return

        ghost = QToolButton(self)
        ghost.setObjectName("edgeDroneRunSummonGhost")
        ghost.setIcon(QIcon(str(media_path("drone_bot.svg"))))
        ghost.setIconSize(QSize(12, 12))
        ghost.setFixedSize(18, 18)
        ghost.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        ghost.setStyleSheet(
            "QToolButton#edgeDroneRunSummonGhost {"
            "  background: #14303a;"
            "  border: 1px solid #7dcfff;"
            "  border-radius: 9px;"
            "  padding: 0px;"
            "}"
        )

        start = self._centered_child_pos(self._drone_tab, ghost.size())
        pip_origin = pip.mapTo(self, QPoint(0, 0))
        end = QPoint(
            pip_origin.x() + (pip.width() - ghost.width()) // 2,
            pip_origin.y() + (pip.height() - ghost.height()) // 2,
        )
        ghost.move(start)
        ghost.show()
        ghost.raise_()

        animation = QPropertyAnimation(ghost, b"pos", self)
        animation.setDuration(540)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.finished.connect(
            lambda rid=run_id, g=ghost, a=animation: self._finish_summon_animation(rid, g, a)
        )
        self._summon_animations[run_id] = (ghost, animation)
        animation.start()

    def _finish_summon_animation(
        self,
        run_id: str,
        ghost: QToolButton,
        animation: QPropertyAnimation,
    ) -> None:
        self._summon_animations.pop(run_id, None)
        animation.deleteLater()
        ghost.deleteLater()
        pip = self._drone_run_pips.get(run_id)
        if pip is not None:
            pip.finish_launch_animation()

    @staticmethod
    def _centered_child_pos(child: QWidget, target_size: QSize) -> QPoint:
        rect = child.geometry()
        return QPoint(
            rect.center().x() - target_size.width() // 2,
            rect.center().y() - target_size.height() // 2,
        )

    @staticmethod
    def _normalize_drone_state(state: str) -> str:
        normalized = state.strip().lower().replace(" ", "_").replace("-", "_")
        if normalized in {"waiting", "approval", "waiting_approval"}:
            return "waiting_for_approval"
        if normalized in {"done", "success"}:
            return "completed"
        if normalized == "error":
            return "failed"
        return normalized

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

    def _drone_tab_style(self) -> str:
        return (
            "QToolButton#edgeDroneTab {"
            "  background: #161b33;"
            f"  color: {LABEL_FILES};"
            f"  border: 1px solid {ACCENT};"
            "  border-right: none;"
            "  border-top-left-radius: 8px;"
            "  border-bottom-left-radius: 8px;"
            "  border-top-right-radius: 0px;"
            "  border-bottom-right-radius: 0px;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeDroneTab:hover {"
            "  background: #1d2f55;"
            f"  color: {ACCENT_HOVER};"
            f"  border-color: {LABEL_FILES};"
            "  border-right: none;"
            "}"
            "QToolButton#edgeDroneTab:checked {"
            "  background: #221b44;"
            f"  color: {LABEL_PROJECTS};"
            f"  border-color: {LABEL_PROJECTS};"
            "  border-right: none;"
            "}"
            "QToolButton#edgeDroneTab:checked:hover {"
            "  background: #2a245f;"
            f"  color: {FG};"
            f"  border-color: {ACCENT_HOVER};"
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

    def _companion_tab_style(self, accent: str = FG_DIM, bg: str = "#161b33") -> str:
        return (
            "QToolButton#edgeCompanionTab {"
            f"  background: {bg};"
            f"  color: {accent};"
            f"  border: 1px solid {accent};"
            "  border-right: none;"
            "  border-top-left-radius: 8px;"
            "  border-bottom-left-radius: 8px;"
            "  border-top-right-radius: 0px;"
            "  border-bottom-right-radius: 0px;"
            "  padding: 0px;"
            "}"
            "QToolButton#edgeCompanionTab:hover {"
            f"  background: #1d2f55;"
            f"  color: {ACCENT_HOVER};"
            f"  border-color: {ACCENT_HOVER};"
            "  border-right: none;"
            "}"
        )
