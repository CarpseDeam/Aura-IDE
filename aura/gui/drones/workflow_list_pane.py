from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.drones.chain_store import ChainStore
from aura.drones.store import DroneStore
from aura.gui.theme import ACCENT, BG, BG_RAISED, BORDER, DANGER, FG, FG_MUTED, SUCCESS

logger = logging.getLogger(__name__)


class WorkflowListPane(QWidget):
    """Lists saved Chains (workflows) with Run/Edit actions."""

    runWorkflowRequested = Signal(str)  # chain_id
    editWorkflowRequested = Signal(str)  # chain_id
    deleteWorkflowRequested = Signal(str)  # chain_id
    newWorkflowRequested = Signal()

    def __init__(
        self, workspace_root: Path | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root

        self.setObjectName("workflowListPane")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._container = QWidget()
        self._container.setObjectName("workflowContainer")
        self._container.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)

        layout.addWidget(self._container, 1)

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root

    def refresh(self) -> None:
        """Rebuild the workflow list from the chain store."""
        # Clear existing rows
        while self._layout.count() > 0:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._workspace_root is None:
            self._show_empty_state()
            return

        chains = ChainStore.list_chains(self._workspace_root)
        if not chains:
            self._show_empty_state()
            return

        # Build a drone lookup: drone_id -> name
        drones = DroneStore.list_drones(self._workspace_root)
        drone_names: dict[str, str] = {d.id: d.name for d in drones}

        # Lazy import to avoid circular dependency at module load time.
        from aura.drones.chain_runner import get_last_chain_run  # noqa: E402

        for chain in chains:
            last_run = get_last_chain_run(self._workspace_root, chain.id)
            row = self._build_chain_row(chain, drone_names, last_run)
            self._layout.addWidget(row)

        self._layout.addStretch(1)

    def _show_empty_state(self) -> None:
        while self._layout.count() > 0:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._layout.addStretch(2)

        label1 = QLabel("No workflows yet.")
        label1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label1.setStyleSheet(
            f"font-size: 14px; font-weight: 700; color: {FG};"
            f" background: transparent; padding: 0 16px;"
        )
        self._layout.addWidget(label1)

        label2 = QLabel(
            "Create a reusable workflow by connecting saved Drones."
        )
        label2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label2.setWordWrap(True)
        label2.setStyleSheet(
            f"font-size: 12px; color: {FG_MUTED}; background: transparent;"
            f" padding: 4px 16px 8px 16px;"
        )
        self._layout.addWidget(label2)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        create_btn = QPushButton("Create a Workflow")
        create_btn.setObjectName("primary")
        create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        create_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        create_btn.setStyleSheet(
            f"QPushButton#primary {{ background: {ACCENT}; color: {BG};"
            f" border: 1px solid {ACCENT}; border-radius: 6px;"
            f" padding: 8px 20px; font-weight: 600; font-size: 13px; }}"
            f"QPushButton#primary:hover {{ background: #94b6ff; }}"
        )
        create_btn.clicked.connect(self.newWorkflowRequested.emit)
        btn_row.addWidget(create_btn)
        btn_row.addStretch(1)
        self._layout.addLayout(btn_row)

        self._layout.addStretch(3)

    @staticmethod
    def _relative_time(iso_string: str) -> str:
        """Convert an ISO timestamp to a short relative time string."""
        if not iso_string:
            return "Unknown"
        import datetime as dt
        try:
            ts = dt.datetime.fromisoformat(iso_string)
        except (ValueError, TypeError):
            return "Unknown"
        now = dt.datetime.now(dt.timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        delta = now - ts
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return "Just now"
        if seconds < 60:
            return "Just now"
        if seconds < 3600:
            minutes = seconds // 60
            return f"{minutes} min ago"
        if seconds < 86400:
            hours = seconds // 3600
            return f"{hours}h ago"
        if seconds < 172800:
            return "Yesterday"
        return ts.strftime("%b %d")

    def _build_chain_row(
        self, chain, drone_names: dict[str, str], last_run=None
    ) -> QWidget:
        row = QFrame()
        row.setObjectName("workflowRow")
        row.setFixedHeight(44)
        row.setStyleSheet(
            "QFrame#workflowRow {"
            "  background: transparent;"
            f" border-bottom: 1px solid {BORDER};"
            "  padding: 0px;"
            "}"
            "QFrame#workflowRow:hover {"
            "  background: rgba(255,255,255,0.03);"
            "}"
        )

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 0, 6, 0)
        row_layout.setSpacing(6)
        row_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # -- Name (fixed width) --
        name_label = QLabel(chain.name)
        name_label.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {FG};"
            " background: transparent;"
        )
        name_label.setFixedWidth(140)
        name_label.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(name_label)

        # -- Node sequence (drone names joined by →) --
        if not chain.nodes:
            seq_text = "(empty)"
            seq_color = FG_MUTED
        else:
            parts: list[str] = []
            has_missing = False
            for node in chain.nodes:
                name = drone_names.get(node.drone_id)
                if name:
                    parts.append(name)
                else:
                    parts.append("(missing)")
                    has_missing = True
            seq_text = " → ".join(parts)
            seq_color = DANGER if has_missing else FG_MUTED

        seq_label = QLabel(seq_text)
        seq_label.setStyleSheet(
            f"font-size: 12px; color: {seq_color}; background: transparent;"
        )
        seq_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        seq_label.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(seq_label, 1)

        # -- Last-run info --
        last_run_container = QWidget()
        last_run_container.setContentsMargins(0, 0, 0, 0)
        last_run_layout = QVBoxLayout(last_run_container)
        last_run_layout.setContentsMargins(0, 0, 0, 0)
        last_run_layout.setSpacing(1)
        last_run_container.setStyleSheet("background: transparent;")

        if last_run is None:
            time_label = QLabel("Never")
            time_label.setStyleSheet(
                f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
            )
            last_run_layout.addWidget(time_label)
        else:
            ts = last_run.ended_at or last_run.started_at
            time_label = QLabel(self._relative_time(ts))
            time_label.setStyleSheet(
                f"font-size: 11px; color: {FG}; background: transparent;"
            )
            last_run_layout.addWidget(time_label)

            # Status chip
            status = last_run.status
            if status == "completed":
                chip_color = SUCCESS
                chip_text = "Done"
            elif status == "failed":
                chip_color = DANGER
                chip_text = "Failed"
            elif status == "stopped":
                chip_color = "#FF9800"
                chip_text = "Stopped"
            else:
                chip_color = FG_MUTED
                chip_text = status.capitalize()

            chip = QLabel(chip_text)
            chip.setStyleSheet(
                f"font-size: 10px; font-weight: 700; color: {chip_color};"
                f" background: transparent; padding: 0px;"
            )
            last_run_layout.addWidget(chip)

        row_layout.addWidget(last_run_container)

        # -- Run button --
        run_btn = QPushButton("Run")
        run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        run_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: {BG};"
            f" border: 1px solid {ACCENT}; border-radius: 4px;"
            f" padding: 2px 10px; font-size: 11px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: #94b6ff; }}"
        )
        run_btn.clicked.connect(
            lambda checked=False, cid=chain.id: self.runWorkflowRequested.emit(cid)
        )
        row_layout.addWidget(run_btn)

        # -- Edit button --
        edit_btn = QPushButton("Edit")
        edit_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG};"
            f" border: 1px solid {BORDER}; border-radius: 4px;"
            f" padding: 2px 10px; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {BG_RAISED}; border-color: {ACCENT}; }}"
        )
        edit_btn.clicked.connect(
            lambda checked=False, cid=chain.id: self.editWorkflowRequested.emit(cid)
        )
        row_layout.addWidget(edit_btn)

        return row
