"""Compact status card for a worker plan being written."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
)

from aura.conversation.workflow_state import WorkflowState, WorkflowStatus
from aura.gui.cards._helpers import _fade_in_widget
from aura.gui.theme import BG_TOOL_CARD, BORDER, DANGER, SUCCESS, WARN


class PlanWriterCard(QFrame):
    """Small status indicator for temporary planner output.

    Renders from the canonical WorkflowState snapshot received via
    update_workflow_state().  The set_result() method is kept for backward
    compat with chat_view controller wiring but no longer drives state —
    the backend _DispatchProxy owns all transitions.
    """

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"
    STATE_PHASE = "phase"
    STATE_INCOMPLETE = "incomplete"
    STATE_NOT_STARTED = "not_started"
    STATE_RETRYING = "retrying"
    STATE_REPAIRING = "repairing"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("planWriterCard")
        self.setMinimumWidth(0)
        self.setMinimumHeight(26)
        self.setMaximumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._goal: str = ""
        self._latest_spec: str = ""
        self._state = self.STATE_RUNNING
        self._has_workflow_snapshot: bool = False
        self._incomplete_text = "⚡ Plan incomplete"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(0)

        self._status = QLabel(self)
        self._status.setObjectName("planWriterStatus")
        self._status.setMinimumWidth(0)
        self._status.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self._status)

        self._refresh()
        _fade_in_widget(self)

    def _refresh(self) -> None:
        state_color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS,
            self.STATE_FAILED: DANGER,
            self.STATE_PHASE: WARN,
            self.STATE_INCOMPLETE: WARN,
            self.STATE_NOT_STARTED: WARN,
            self.STATE_RETRYING: WARN,
            self.STATE_REPAIRING: WARN,
        }[self._state]

        text = {
            self.STATE_RUNNING: (
                f"⚡ Writing plan: {self._goal}" if self._goal else "⚡ Writing plan..."
            ),
            self.STATE_DONE: "⚡ Plan ready ✓",
            self.STATE_FAILED: "⚡ Plan failed ✗",
            self.STATE_PHASE: "⚡ Phase complete — preparing follow-up",
            self.STATE_INCOMPLETE: self._incomplete_text,
            self.STATE_NOT_STARTED: self._incomplete_text,
            self.STATE_RETRYING: "⚡ Repairing plan…",
            self.STATE_REPAIRING: "⚡ Repairing plan…",
        }[self._state]

        metrics = QFontMetrics(self._status.font())
        available = max(40, self._status.width() or self.width() - 20)
        self._status.setText(
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, available)
        )

        tooltip_parts = []
        if self._goal:
            tooltip_parts.append(f"Goal: {self._goal}")
        if self._latest_spec:
            tooltip_parts.append(f"Latest spec:\n{self._latest_spec[:2000]}")
        tooltip = "\n\n".join(tooltip_parts) or "Writing plan"
        self.setToolTip(tooltip)
        self._status.setToolTip(tooltip)

        self.setStyleSheet(
            f"""
            QFrame#planWriterCard {{
                background: {BG_TOOL_CARD};
                border: 1px solid {BORDER};
                border-left: 2px solid {state_color};
                border-radius: 8px;
            }}
            QLabel#planWriterStatus {{
                color: {state_color};
                font-size: 12px;
                font-weight: 600;
            }}
            """
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()

    def set_goal(self, goal: str) -> None:
        self._goal = goal
        self._refresh()

    def update_spec(self, spec: str) -> None:
        """Store the latest streamed spec without displaying it inline."""
        self._latest_spec = spec
        # No _refresh() — the visible card does not display the spec,
        # so per-fragment refresh is wasted work.

    def set_result(self, ok: bool, result_text: str | None = None) -> None:
        """Legacy fallback when no WorkflowState snapshot ever arrived.

        Once update_workflow_state has been called (backend owns the state),
        this method is a no-op — it must never override the canonical snapshot.
        """
        if self._has_workflow_snapshot:
            return
        # Basic done/failed — dispatch-specific states (rejected, cancelled,
        # internal_continuation, phase_boundary) are now owned by the backend
        # _DispatchProxy and rendered via update_workflow_state.
        self._state = self.STATE_DONE if ok else self.STATE_FAILED
        self._refresh()

    def set_stream_state(self, state: str) -> None:
        """Project the thin controller fallback when no backend snapshot exists."""
        if self._has_workflow_snapshot:
            return
        if state == "repairing":
            self._state = self.STATE_REPAIRING
        elif state == "done":
            self._state = self.STATE_DONE
        elif state == "failed":
            self._state = self.STATE_FAILED
        else:
            self._state = self.STATE_RUNNING
        self._refresh()

    def update_workflow_state(self, state: WorkflowState) -> None:
        """Render from the canonical backend WorkflowState snapshot.

        Mapping from WorkflowStatus:
          plan_ready          → done (plan written)
          dispatched/editing/validating → running
          blocked             → incomplete
          failed_retryable    → retrying
          failed_nonrecoverable → failed
          done                → done
          cancelled           → not_started (cancelled)
        """
        self._has_workflow_snapshot = True
        old_state = self._state
        if state.status == WorkflowStatus.plan_ready:
            self._state = self.STATE_DONE
        elif state.status in {
            WorkflowStatus.dispatched,
            WorkflowStatus.editing,
            WorkflowStatus.validating,
        }:
            self._state = self.STATE_RUNNING
        elif state.status == WorkflowStatus.blocked:
            self._state = self.STATE_INCOMPLETE
            self._incomplete_text = "⚡ Plan incomplete"
        elif state.status == WorkflowStatus.failed_retryable:
            self._state = self.STATE_REPAIRING
        elif state.status == WorkflowStatus.failed_nonrecoverable:
            self._state = self.STATE_FAILED
        elif state.status == WorkflowStatus.done:
            self._state = self.STATE_DONE
        elif state.status == WorkflowStatus.cancelled:
            self._state = self.STATE_NOT_STARTED
            self._incomplete_text = "⚡ Plan cancelled"
        elif state.status == WorkflowStatus.planner_resolving:
            self._state = self.STATE_REPAIRING
        else:
            self._state = self.STATE_RUNNING

        if old_state != self._state:
            self._refresh()
