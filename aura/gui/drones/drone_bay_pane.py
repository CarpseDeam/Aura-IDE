from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.drones.definition import DroneDefinition
from aura.drones.run import DroneRun
from aura.drones.store import DroneStore, RunHistoryStore
from aura.gui.drones.drone_run_card import DroneRunCard
from aura.gui.theme import ACCENT, BG, BG_RAISED, BORDER, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN


class DroneBayPane(QWidget):
    """Panel that displays saved Drones with action buttons.
    
    Future phases will add live Drone run cards (active run pips),
    parallel execution indicators, and receipt display. Keep this
    widget capable of hosting both static definitions and live runs.
    """

    newDroneRequested = Signal()
    editDroneRequested = Signal(str)
    duplicateDroneRequested = Signal(str)
    deleteDroneRequested = Signal(str)
    launchDroneRequested = Signal(str)  # drone_id
    activeRunFocusRequested = Signal()
    viewRunReceiptRequested = Signal(str)  # run_id

    def __init__(self, workspace_root: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._active_run: DroneRun | None = None
        self._active_run_card: DroneRunCard | None = None
        self._history_section: QWidget | None = None
        self._run_history_widgets: list[QWidget] = []
        self._scroll_content = self._card_layout

        self.setObjectName("droneBayPane")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # -- Header --
        header = QWidget(self)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(2)

        title = QLabel("Drones")
        title.setObjectName("droneBayTitle")
        title.setStyleSheet(
            f"font-size: 18px; font-weight: 700; color: {FG}; background: transparent;"
        )
        header_layout.addWidget(title)

        subtitle = QLabel(
            "Small focused workers Aura can save and launch for repeatable tasks."
        )
        subtitle.setObjectName("droneBaySubtitle")
        subtitle.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent;"
        )
        subtitle.setWordWrap(True)
        header_layout.addWidget(subtitle)

        new_btn = QPushButton("+ New Drone")
        new_btn.setObjectName("primary")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(
            f"QPushButton#primary {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 16px; font-weight: 600; font-size: 13px; }}"
            f"QPushButton#primary:hover {{ background: #94b6ff; }}"
        )
        new_btn.clicked.connect(self.newDroneRequested.emit)
        header_layout.addWidget(new_btn)

        layout.addWidget(header)

        # -- Scrollable card area --
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setObjectName("droneBayScroll")
        scroll.setStyleSheet(
            "QScrollArea#droneBayScroll { background: transparent; border: none; }"
        )

        self._card_container = QWidget()
        self._card_container.setObjectName("droneCardContainer")
        self._card_container.setStyleSheet("background: transparent;")
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(8)
        self._card_layout.addStretch(1)

        scroll.setWidget(self._card_container)
        layout.addWidget(scroll, 1)

        self.refresh()

    # -- Public API --

    def refresh(self) -> None:
        """Reload drones from DroneStore and rebuild cards + run history."""
        # Clear existing history section first
        self._clear_history_section()
        # Clear existing drone cards
        while self._card_layout.count() > 0:
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._workspace_root is None:
            self._show_empty_state()
            return

        drones = DroneStore.list_drones(self._workspace_root)
        if not drones:
            self._show_empty_state()
            return

        for drone in drones:
            card = self._build_drone_card(drone)
            self._card_layout.addWidget(card)

        # Add run history section after drone cards
        self.refresh_run_history()

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root
        self.refresh()

    def set_active_run(self, run: DroneRun | None, card: DroneRunCard | None = None) -> None:
        """Track the currently active Drone run and its card."""
        self._active_run = run
        self._active_run_card = card

    def has_active_run(self) -> bool:
        """Return True if there is an active Drone run."""
        return self._active_run is not None and self._active_run.is_active

    # -- Internal helpers --

    def _show_empty_state(self) -> None:
        # Remove any existing items first
        while self._card_layout.count() > 0:
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._card_layout.addStretch(2)
        empty = QLabel(
            "No drones yet.\n\n"
            "Create one from scratch, or save a useful Aura workflow later."
        )
        empty.setObjectName("droneBayEmpty")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setWordWrap(True)
        empty.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 13px; padding: 16px; "
            f"background: transparent;"
        )
        self._card_layout.addWidget(empty)
        self._card_layout.addStretch(3)

    def _build_drone_card(self, drone: DroneDefinition) -> QFrame:
        card = QFrame()
        card.setObjectName("droneCard")
        card.setStyleSheet(
            f"QFrame#droneCard {{ background: {BG_RAISED}; "
            f"border: 1px solid {BORDER}; border-radius: 8px; "
            f"padding: 0px; }}"
            f"QFrame#droneCard:hover {{ background: #2b2b34; "
            f"border-color: {ACCENT}; }}"
        )

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)

        # Name
        name_label = QLabel(drone.name)
        name_label.setStyleSheet(
            f"font-size: 14px; font-weight: 700; color: {FG}; background: transparent;"
        )
        card_layout.addWidget(name_label)

        # Description
        desc_label = QLabel(drone.description)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent;"
        )
        card_layout.addWidget(desc_label)

        # Badge row
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 2, 0, 2)
        badge_row.setSpacing(6)

        write_badge = self._make_policy_badge(drone.write_policy)
        badge_row.addWidget(write_badge)

        budget_label = QLabel(
            f"{drone.budget.max_tool_rounds} rounds · "
            f"{drone.budget.timeout_seconds // 60} min"
        )
        budget_label.setStyleSheet(
            f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
        )
        badge_row.addWidget(budget_label)
        badge_row.addStretch(1)

        card_layout.addLayout(badge_row)

        # Action buttons
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 4, 0, 0)
        action_row.setSpacing(6)

        launch_btn = QPushButton("Launch")
        if drone.write_policy == "read_only":
            launch_btn.setToolTip(f"Launch {drone.name}")
        elif drone.write_policy == "ask_before_writes":
            launch_btn.setToolTip(f"Launch {drone.name} (asks before writes)")
        elif drone.write_policy == "normal_diff_approval":
            launch_btn.setToolTip(f"Launch {drone.name} (diff approval)")
        launch_btn.setEnabled(True)
        launch_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 4px; "
            f"padding: 3px 14px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:disabled {{ background: #2a2a30; color: #555566; "
            f"border: 1px solid #333340; }}"
        )
        launch_btn.clicked.connect(
            lambda checked=False, did=drone.id: self.launchDroneRequested.emit(did)
        )
        action_row.addWidget(launch_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 4px; "
            f"padding: 3px 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {BG_RAISED}; border-color: {ACCENT}; }}"
        )
        edit_btn.clicked.connect(lambda checked=False, did=drone.id: self.editDroneRequested.emit(did))
        action_row.addWidget(edit_btn)

        dup_btn = QPushButton("Duplicate")
        dup_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 4px; "
            f"padding: 3px 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {BG_RAISED}; border-color: {ACCENT}; }}"
        )
        dup_btn.clicked.connect(lambda checked=False, did=drone.id: self.duplicateDroneRequested.emit(did))
        action_row.addWidget(dup_btn)

        del_btn = QPushButton("Delete")
        del_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {DANGER}; "
            f"border: 1px solid {DANGER}; border-radius: 4px; "
            f"padding: 3px 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: rgba(247, 118, 142, 0.10); }}"
        )
        del_btn.clicked.connect(lambda checked=False, did=drone.id: self.deleteDroneRequested.emit(did))
        action_row.addWidget(del_btn)

        card_layout.addLayout(action_row)
        return card

    def _make_policy_badge(self, write_policy: str) -> QLabel:
        if write_policy == "read_only":
            text = "Read-only"
            color = WARN
        elif write_policy == "ask_before_writes":
            text = "Ask before writes"
            color = ACCENT
        elif write_policy == "normal_diff_approval":
            text = "Normal diff approval"
            color = SUCCESS
        else:
            text = write_policy
            color = FG_DIM

        badge = QLabel(text)
        badge.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {color}; "
            f"background: transparent; padding: 1px 0;"
        )
        return badge

    # -- Run History section --

    def refresh_run_history(self) -> None:
        """Reload run history from disk and rebuild the UI section."""
        self._clear_history_section()
        self._run_history_widgets.clear()

        if self._workspace_root is None:
            return

        runs = RunHistoryStore.list_runs(self._workspace_root)
        if not runs:
            return

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.1); max-height: 1px;")
        self._card_layout.addWidget(sep)

        self._history_section = QWidget()
        section_layout = QVBoxLayout(self._history_section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(4)

        # Header row: "RUN HISTORY" label + clear button
        header_row = QHBoxLayout()
        history_label = QLabel("RUN HISTORY")
        history_label.setStyleSheet(
            "font-size: 11px; font-weight: 600; color: rgba(255,255,255,0.5); letter-spacing: 1px; padding: 8px 0 4px 12px;"
        )
        header_row.addWidget(history_label)
        header_row.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedSize(60, 24)
        clear_btn.setStyleSheet(
            "QPushButton {"
            "  background: transparent; color: rgba(255,255,255,0.4);"
            "  border: 1px solid rgba(255,255,255,0.15); border-radius: 4px;"
            "  font-size: 11px; padding: 2px 8px;"
            "}"
            "QPushButton:hover { background: rgba(255,255,255,0.1); color: #fff; }"
        )
        clear_btn.clicked.connect(self._clear_run_history)
        header_row.addWidget(clear_btn)
        section_layout.addLayout(header_row)

        # Run history cards
        for run_data in runs:
            card = self._build_history_card(run_data)
            section_layout.addWidget(card)
            self._run_history_widgets.append(card)

        self._card_layout.addWidget(self._history_section)

    def _build_history_card(self, run_data: dict) -> QFrame:
        """Build a compact clickable card for a single run entry."""
        card = QFrame()
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setStyleSheet(
            "QFrame {"
            "  background: rgba(255,255,255,0.04); border-radius: 6px;"
            "  padding: 8px 12px; margin: 2px 10px;"
            "}"
            "QFrame:hover { background: rgba(255,255,255,0.08); }"
        )
        layout = QHBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(12)

        # Status icon
        status = run_data.get("status", "unknown")
        if status == "completed":
            icon_text = "\u2713"
            icon_color = "#4CAF50"
        elif status == "failed":
            icon_text = "\u2717"
            icon_color = "#F44336"
        else:
            icon_text = "\u25D0"
            icon_color = "#FF9800"

        icon = QLabel(icon_text)
        icon.setStyleSheet(f"color: {icon_color}; font-size: 16px; font-weight: bold;")
        icon.setFixedWidth(24)
        layout.addWidget(icon)

        # Info column
        info_col = QVBoxLayout()
        info_col.setSpacing(2)

        name_label = QLabel(run_data.get("drone_name", "Unknown"))
        name_label.setStyleSheet("color: #fff; font-size: 13px; font-weight: 500;")
        info_col.addWidget(name_label)

        # Detail row: timestamp | duration | tool calls
        started_at = run_data.get("started_at", "")
        elapsed = run_data.get("elapsed_seconds", 0)
        tool_count = len(run_data.get("tool_calls", []))

        # Format timestamp
        try:
            import datetime
            ts = datetime.datetime.fromisoformat(started_at)
            time_str = ts.strftime("%Y-%m-%d %H:%M")
        except Exception:
            time_str = started_at[:16] if started_at else "?"

        duration_str = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed/60:.1f}m"
        detail = QLabel(f"{time_str}  |  {duration_str}  |  {tool_count} call{'s' if tool_count != 1 else ''}")
        detail.setStyleSheet("color: rgba(255,255,255,0.45); font-size: 11px;")
        info_col.addWidget(detail)

        layout.addLayout(info_col, stretch=1)
        layout.addStretch()

        # Make clickable
        run_id = run_data.get("run_id", "")
        card.mousePressEvent = lambda event, rid=run_id: self._on_history_card_clicked(event, rid)

        return card

    def _on_history_card_clicked(self, event, run_id: str) -> None:
        self.viewRunReceiptRequested.emit(run_id)

    def _clear_run_history(self) -> None:
        if self._workspace_root is not None:
            RunHistoryStore.clear_history(self._workspace_root)
            self.refresh_run_history()

    def _clear_history_section(self) -> None:
        """Remove the history section and separator from the layout."""
        if self._history_section is not None:
            self._card_layout.removeWidget(self._history_section)
            self._history_section.deleteLater()
            self._history_section = None
        self._run_history_widgets.clear()
