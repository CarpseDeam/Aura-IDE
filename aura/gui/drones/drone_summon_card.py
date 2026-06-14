"""User confirmation card for Aura-summoned Drone runs."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from aura.drones.definition import DroneDefinition
from aura.gui.theme import ACCENT, BG, BG_RAISED, BORDER, DANGER, FG, FG_DIM, SUCCESS


class DroneSummonCard(QFrame):
    """Shows Aura's Drone suggestion before any run starts."""

    summonRequested = Signal(str)
    cancelRequested = Signal(str)

    def __init__(
        self,
        *,
        request_id: str,
        drone: DroneDefinition,
        goal: str,
        reason: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._request_id = request_id
        self._drone = drone
        self._goal = goal
        self._reason = reason
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("droneSummonCard")
        self.setStyleSheet(
            f"QFrame#droneSummonCard {{ background: {BG_RAISED}; border: 1px solid {ACCENT}; "
            "border-radius: 8px; padding: 0px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title = QLabel(f"Aura wants to summon {self._drone.name}.")
        title.setStyleSheet(f"color: {FG}; font-size: 14px; font-weight: 700; background: transparent;")
        title.setWordWrap(True)
        layout.addWidget(title)

        goal = QLabel(f"<b>Goal:</b> {self._goal}")
        goal.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent;")
        goal.setWordWrap(True)
        layout.addWidget(goal)

        if self._reason:
            reason = QLabel(f"<b>Why:</b> {self._reason}")
            reason.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent;")
            reason.setWordWrap(True)
            layout.addWidget(reason)

        budget_min = max(1, self._drone.budget.timeout_seconds // 60)
        meta = QLabel(
            f"Scope: {self._policy_label(self._drone.write_policy)}. "
            f"Timeout: {budget_min} min."        )
        meta.setStyleSheet(f"color: {SUCCESS}; font-size: 11px; background: transparent;")
        meta.setWordWrap(True)
        layout.addWidget(meta)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        summon_btn = QPushButton("Summon")
        summon_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: {BG}; border: 1px solid {ACCENT}; "
            "border-radius: 4px; padding: 4px 16px; font-size: 12px; font-weight: 600; }}"
            "QPushButton:hover { background: #94b6ff; }"
        )
        summon_btn.clicked.connect(lambda: self.summonRequested.emit(self._request_id))
        actions.addWidget(summon_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {DANGER}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 4px 16px; font-size: 12px; }}"
            "QPushButton:hover { background: rgba(247, 118, 142, 0.10); }"
        )
        cancel_btn.clicked.connect(lambda: self.cancelRequested.emit(self._request_id))
        actions.addWidget(cancel_btn)
        actions.addStretch(1)

        layout.addLayout(actions)

    @staticmethod
    def _policy_label(write_policy: str) -> str:
        if write_policy == "read_only":
            return "read-only"
        if write_policy == "ask_before_writes":
            return "asks before writes"
        if write_policy == "normal_diff_approval":
            return "normal diff approval"
        return write_policy
