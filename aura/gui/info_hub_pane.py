"""Info hub pane: checklist-only Progress panel.

Shows Aura's live Task Checklist, the run status chip, and the Stop button —
nothing else. Execution reasoning/content, the activity heartbeat, and
per-write diff/error cards are not rendered here; they either duplicate what
chat already shows (reasoning/content stream chat-owned) or belong to the
authoritative code editor / floating terminal instead.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.gui.execution_finish_outcome import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_ERROR,
    terminal_status_label,
)
from aura.gui.theme import BG, BORDER, DANGER, FG_MUTED, SUCCESS
from aura.gui.widgets.task_checklist import TaskChecklistWidget

_log = logging.getLogger(__name__)

#: Progress pane status chip texts. The chip is the UI's only claim about
#: whether a run is still going or how it ended, so every state is named
#: rather than spelled inline: a run that has stopped must never be left
#: reading "Live", and a terminal state must always be truthful about outcome.
_CHIP_IDLE = "Idle"
_CHIP_LIVE = "● Live"

_CHIP_STYLE_BY_LABEL = {
    STATUS_DONE: f"color: {SUCCESS}; font-size: 10px; font-weight: 600;",
    STATUS_CANCELLED: f"color: {FG_MUTED}; font-size: 10px; font-weight: 600;",
    STATUS_ERROR: f"color: {DANGER}; font-size: 10px; font-weight: 600;",
}


class InfoHubPane(QWidget):
    """Bottom pane: PROGRESS header, Task Checklist, status chip, Stop button.

    Public API:
        update_task_checklist(items) -> None
        set_terminal_status(ok, status) -> None
        set_execution_running(running) -> None
        clear_log() -> None
        clear() -> None
    """

    stop_execution_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setObjectName("infoHubPane")
        self.setStyleSheet(
            f"QWidget#infoHubPane {{ background: {BG}; border-top: 1px solid {BORDER}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header row
        self._header = QWidget(self)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(10, 4, 10, 4)
        header_layout.setSpacing(8)

        header_title = QLabel("PROGRESS")
        header_title.setObjectName("paneTitleWorkspace")
        header_layout.addWidget(header_title)

        # Status chip
        self._status_chip = QLabel("")
        self._status_chip.setObjectName("executionLogStatusChip")
        header_layout.addWidget(self._status_chip)

        header_layout.addStretch(1)

        # Stop button (cancels active run, clears queue, preserves draft)
        self._stop_execution_btn = QPushButton("Stop")
        self._stop_execution_btn.setObjectName("danger")
        self._stop_execution_btn.setMinimumSize(44, 36)
        self._stop_execution_btn.setVisible(False)
        self._stop_execution_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_execution_btn.clicked.connect(self._on_stop_execution_clicked)
        header_layout.addWidget(self._stop_execution_btn)

        layout.addWidget(self._header, 0)

        self._todo_widget = TaskChecklistWidget(self)
        layout.addWidget(self._todo_widget, 0)

        layout.addStretch(1)

        self._set_chip_idle()

    # Public API

    def update_task_checklist(self, items: list[dict[str, str]]) -> None:
        """Render the latest Task Checklist snapshot."""
        self._todo_widget.update_snapshot(items)

    def set_terminal_status(
        self,
        ok: bool,
        status: str | None,
    ) -> None:
        """Set the truthful terminal status chip for a finished/cancelled run."""
        label = terminal_status_label(ok=ok, status=status)
        self._status_chip.setText(label)
        self._status_chip.setStyleSheet(_CHIP_STYLE_BY_LABEL[label])

    def _on_stop_execution_clicked(self) -> None:
        """Click handler: disable button, show stopping text, emit signal."""
        self._stop_execution_btn.setEnabled(False)
        self._stop_execution_btn.setText("Stopping...")
        self.stop_execution_requested.emit()

    def set_execution_running(self, running: bool) -> None:
        """Show/hide the Stop button and own the Live claim on the status chip.

        Stopping is the authoritative end of the Live state: whatever path got
        here — terminal status, surfaced error, mismatch, cancellation, or an
        execution thread that simply exited — the chip must stop claiming the
        run is going. A terminal chip already written by
        :meth:`set_terminal_status` is left alone so the truthful outcome
        survives, which also makes repeated calls idempotent.
        """
        self._stop_execution_btn.setVisible(running)
        if running:
            self._stop_execution_btn.setEnabled(True)
            self._stop_execution_btn.setText("Stop")
            self._status_chip.setText(_CHIP_LIVE)
            self._status_chip.setStyleSheet(f"color: {SUCCESS}; font-size: 10px; font-weight: 600;")
        else:
            self._stop_execution_btn.setVisible(False)
            self._stop_execution_btn.setEnabled(True)
            self._stop_execution_btn.setText("Stop")
            if self._status_chip.text() == _CHIP_LIVE:
                self._set_chip_idle()

    def _set_chip_idle(self) -> None:
        self._status_chip.setText(_CHIP_IDLE)
        self._status_chip.setStyleSheet(f"color: {FG_MUTED}; font-size: 10px;")

    def clear(self) -> None:
        """Reset the Progress pane: clear the checklist and status."""
        _log.info("DIAGNOSTIC InfoHubPane.clear called")
        self.clear_log()

    def clear_log(self) -> None:
        """Clear the checklist and status chip ahead of a new execution."""
        _log.info("DIAGNOSTIC InfoHubPane.clear_log called — clearing checklist and status")
        self._todo_widget.clear()
        self._set_chip_idle()
