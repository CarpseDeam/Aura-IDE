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

from aura.drones.badges import compute_capability_badges
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
    buildDroneRequested = Signal()
    editDroneRequested = Signal(str)
    duplicateDroneRequested = Signal(str)
    deleteDroneRequested = Signal(str)
    launchDroneRequested = Signal(str)  # drone_id
    makeToolRequested = Signal(str)
    activeRunFocusRequested = Signal()
    viewRunReceiptRequested = Signal(str)  # run_id

    def __init__(self, workspace_root: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._active_run: DroneRun | None = None
        self._active_run_card: DroneRunCard | None = None
        self._history_section: QWidget | None = None
        self._history_separator: QFrame | None = None
        self._run_history_widgets: list[QWidget] = []

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

        design_btn = QPushButton("Build a Drone")
        design_btn.setObjectName("secondary")
        design_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        design_btn.setStyleSheet(
            f"QPushButton#secondary {{ background: transparent; color: {ACCENT}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 16px; font-weight: 600; font-size: 13px; }}"
            f"QPushButton#secondary:hover {{ background: rgba(122, 162, 247, 0.10); }}"
        )
        design_btn.clicked.connect(self.buildDroneRequested.emit)
        header_layout.addWidget(design_btn)

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
        self._card_layout.setSpacing(2)
        self._card_layout.addStretch(1)

        scroll.setWidget(self._card_container)
        layout.addWidget(scroll, 1)

        self.refresh()

    # -- Public API --

    def refresh(self) -> None:
        """Reload drones from DroneStore and rebuild rows + run history."""
        # Clear existing history section first
        self._clear_history_section()
        # Clear existing drone rows
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

        # Build last-run lookup: map drone_id -> most recent run data
        all_runs = RunHistoryStore.list_runs(self._workspace_root)
        last_run_by_drone: dict[str, dict] = {}
        for run_data in all_runs:
            did = run_data.get("drone_id", "")
            if did and did not in last_run_by_drone:
                last_run_by_drone[did] = run_data

        for drone in drones:
            last_run_info = last_run_by_drone.get(drone.id)
            row_widget = self._build_drone_row(drone, last_run_info)
            self._card_layout.addWidget(row_widget)

        self._card_layout.addStretch(1)


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
            "Create one from scratch, or build one with Aura and refine it later."
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

    def _make_policy_badge(self, write_policy: str, compact: bool = False) -> QLabel:
        if write_policy == "read_only":
            text = "Read"
            tooltip = "Read-only — Drone can inspect files but not write"
            color = WARN
        elif write_policy == "ask_before_writes":
            text = "Ask"
            tooltip = "Ask writes — Aura asks before writing"
            color = ACCENT
        elif write_policy == "normal_diff_approval":
            text = "Diff"
            tooltip = "Diff approval — Aura asks before writing"
            color = SUCCESS
        else:
            text = write_policy
            tooltip = ""
            color = FG_DIM

        badge = QLabel(text)
        badge.setToolTip(tooltip)
        badge.setStyleSheet(
            f"font-size: 9px; font-weight: 600; color: {color}; "
            f"background: transparent; padding: 1px 6px; "
            f"border-radius: 4px;"
        )
        return badge

    def _make_trigger_pill(self) -> QLabel:
        pill = QLabel("Manual")
        pill.setStyleSheet(
            f"font-size: 10px; font-weight: 600; color: {FG_MUTED}; "
            f"background: transparent; padding: 2px 8px; "
            f"border: 1px solid rgba(255,255,255,0.12); border-radius: 4px;"
        )
        return pill

    def _build_drone_row(self, drone: DroneDefinition, last_run_info: dict | None) -> QWidget:
        """Build a compact drone row with an expandable detail panel.

        Returns an outer QWidget containing the always-visible row QFrame
        and the optionally-visible detail QFrame.
        """
        wrapper = QWidget()
        wrapper.setObjectName("droneRowWrapper")
        wrapper.setStyleSheet("background: transparent;")

        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        wrapper.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # ---- Always-visible row ----
        row = QFrame()
        row.setObjectName("droneRow")
        row.setFixedHeight(44)
        row.setStyleSheet(
            f"QFrame#droneRow {{ background: transparent; border-bottom: 1px solid {BORDER}; padding: 0px; }}"
            f"QFrame#droneRow:hover {{ background: rgba(255,255,255,0.03); }}"
        )

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 0, 6, 0)
        row_layout.setSpacing(6)
        row_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # a) Name
        name_label = QLabel(drone.name)
        name_label.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {FG}; background: transparent;"
        )
        name_label.setFixedWidth(120)
        name_label.setMinimumWidth(120)
        name_label.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(name_label)

        # b) Short description (first ~60 chars)
        short_desc = (drone.description[:47] + "...") if len(drone.description) > 60 else drone.description
        desc_label = QLabel(short_desc)
        desc_label.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent;"
        )
        desc_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        desc_label.setElideMode(Qt.TextElideMode.ElideRight)
        desc_label.setToolTip(drone.description)
        desc_label.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(desc_label, 1)

        # c) Write policy pill
        policy_pill = self._make_policy_badge(drone.write_policy, compact=True)
        row_layout.addWidget(policy_pill)

        # d) Trigger pill (Manual placeholder)
        trigger_pill = self._make_trigger_pill()
        row_layout.addWidget(trigger_pill)

        # e) Last run summary
        last_run_label = QLabel()
        last_run_label.setStyleSheet(
            f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
        )
        if last_run_info:
            elapsed = last_run_info.get("elapsed_seconds", 0)
            tool_calls = len(last_run_info.get("tool_calls", []))
            if elapsed < 60:
                dur_str = f"{elapsed:.0f}s"
            else:
                dur_str = f"{elapsed/60:.1f}m"
            last_run_label.setText(f"{dur_str} \u00b7 {tool_calls}")
            status = last_run_info.get("status", "")
            if status == "failed":
                last_run_label.setStyleSheet(
                    f"font-size: 11px; color: {DANGER}; background: transparent;"
                )
        else:
            last_run_label.setText("Never")
            last_run_label.setStyleSheet(
                f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
            )
        last_run_label.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(last_run_label)

        # f) Run button
        run_btn = QPushButton("Run")
        run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        run_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 4px; "
            f"padding: 2px 10px; font-size: 11px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: #94b6ff; }}"
        )
        run_btn.clicked.connect(
            lambda checked=False, did=drone.id: self.launchDroneRequested.emit(did)
        )
        row_layout.addWidget(run_btn)

        # g) Overflow/details button
        detail_btn = QPushButton("\u22EF")
        detail_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        detail_btn.setFixedWidth(28)
        detail_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 4px; "
            f"padding: 2px 6px; font-size: 14px; }}"
            f"QPushButton:hover {{ border-color: {ACCENT}; }}"
        )
        row_layout.addWidget(detail_btn)

        wrapper_layout.addWidget(row)

        # ---- Expandable detail panel ----
        detail_widget = QFrame()
        detail_widget.setObjectName("droneDetail")
        detail_widget.setVisible(False)
        detail_widget.setStyleSheet(
            f"QFrame#droneDetail {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-top: none; border-radius: 0 0 8px 8px; padding: 12px; }}"
        )

        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(12, 8, 12, 12)
        detail_layout.setSpacing(8)

        # a) Full description
        desc_full = QLabel(drone.description)
        desc_full.setWordWrap(True)
        desc_full.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent;"
        )
        detail_layout.addWidget(desc_full)

        # b) Instructions preview
        instr_label = QLabel("Instructions:")
        instr_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {FG}; background: transparent;"
        )
        detail_layout.addWidget(instr_label)
        instr_preview = drone.instructions[:300] + "..." if len(drone.instructions) > 300 else drone.instructions
        instr_text = QLabel(instr_preview)
        instr_text.setWordWrap(True)
        instr_text.setStyleSheet(
            f"font-size: 11px; color: {FG_MUTED}; background: transparent; font-style: italic;"
        )
        detail_layout.addWidget(instr_text)

        # c) Output contract
        if drone.output_contract:
            oc_label = QLabel("Output contract:")
            oc_label.setStyleSheet(
                f"font-size: 11px; font-weight: 600; color: {FG}; background: transparent;"
            )
            detail_layout.addWidget(oc_label)
            oc_text = QLabel(drone.output_contract)
            oc_text.setWordWrap(True)
            oc_text.setStyleSheet(
                f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
            )
            detail_layout.addWidget(oc_text)

        # d) First-run test
        if drone.first_run_test:
            frt_card = QFrame()
            frt_card.setStyleSheet(
                f"background: rgba(255,255,255,0.03); border-radius: 4px; padding: 6px;"
            )
            frt_card_layout = QVBoxLayout(frt_card)
            frt_card_layout.setContentsMargins(8, 6, 8, 6)
            frt_card_layout.setSpacing(2)
            frt_label_title = QLabel("First-run test:")
            frt_label_title.setStyleSheet(
                f"font-size: 11px; font-weight: 600; color: {FG}; background: transparent;"
            )
            frt_card_layout.addWidget(frt_label_title)
            frt_text = QLabel(drone.first_run_test)
            frt_text.setWordWrap(True)
            frt_text.setStyleSheet(
                f"font-size: 11px; color: {FG_MUTED}; background: transparent; font-style: italic;"
            )
            frt_card_layout.addWidget(frt_text)
            detail_layout.addWidget(frt_card)

        # e) Capability badges
        cap_badges = compute_capability_badges(drone)
        if cap_badges:
            cap_row = QHBoxLayout()
            cap_row.setContentsMargins(0, 2, 0, 2)
            cap_row.setSpacing(6)
            for badge_text in cap_badges:
                cap_label = QLabel(badge_text)
                cap_label.setStyleSheet(
                    f"font-size: 10px; color: {FG_MUTED}; background: transparent; "
                    f"padding: 1px 6px; border: 1px solid rgba(255,255,255,0.08); "
                    f"border-radius: 4px;"
                )
                if badge_text == "First-run test available" and drone.first_run_test:
                    cap_label.setToolTip(drone.first_run_test)
                cap_row.addWidget(cap_label)
            cap_row.addStretch(1)
            detail_layout.addLayout(cap_row)

        # f) Budget
        budget_label = QLabel(
            f"{drone.budget.max_tool_rounds} max tool rounds \u00b7 "
            f"{drone.budget.timeout_seconds // 60} min timeout"
        )
        budget_label.setStyleSheet(
            f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
        )
        detail_layout.addWidget(budget_label)

        # g) Action buttons row
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 4, 0, 0)
        action_row.setSpacing(6)

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

        tool_btn = QPushButton("Make Tool")
        tool_btn.setToolTip("Create a .aura/tools scaffold for this Drone")
        tool_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {ACCENT}; "
            f"border: 1px solid {BORDER}; border-radius: 4px; "
            f"padding: 3px 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {BG_RAISED}; border-color: {ACCENT}; }}"
        )
        tool_btn.clicked.connect(lambda checked=False, did=drone.id: self.makeToolRequested.emit(did))
        action_row.addWidget(tool_btn)

        del_btn = QPushButton("Delete")
        del_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {DANGER}; "
            f"border: 1px solid {DANGER}; border-radius: 4px; "
            f"padding: 3px 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: rgba(247, 118, 142, 0.10); }}"
        )
        del_btn.clicked.connect(lambda checked=False, did=drone.id: self.deleteDroneRequested.emit(did))
        action_row.addWidget(del_btn)

        action_row.addStretch(1)
        detail_layout.addLayout(action_row)

        # h) Recent runs for this drone
        recent_label = QLabel("Recent runs")
        recent_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {FG_DIM}; background: transparent;"
        )
        detail_layout.addWidget(recent_label)

        # Query runs filtered to this drone
        if self._workspace_root is not None:
            drone_runs = RunHistoryStore.list_runs(self._workspace_root)
            filtered = [r for r in drone_runs if r.get("drone_id") == drone.id][:5]
        else:
            filtered = []

        if filtered:
            for run_data in filtered:
                run_row = self._build_history_card(run_data)
                detail_layout.addWidget(run_row)
        else:
            no_runs = QLabel("No runs yet")
            no_runs.setStyleSheet(
                f"font-size: 11px; color: {FG_MUTED}; background: transparent; font-style: italic;"
            )
            detail_layout.addWidget(no_runs)

        wrapper_layout.addWidget(detail_widget)

        # ---- Toggle logic ----
        def _toggle_detail() -> None:
            is_open = detail_widget.isVisible()
            detail_widget.setVisible(not is_open)
            # Update styles so row indicates expanded state
            if not is_open:
                row.setStyleSheet(
                    f"QFrame#droneRow {{ background: transparent; border-bottom: 2px solid {ACCENT}; padding: 0px; }}"
                )
            else:
                row.setStyleSheet(
                    f"QFrame#droneRow {{ background: transparent; border-bottom: 1px solid {BORDER}; padding: 0px; }}"
                    f"QFrame#droneRow:hover {{ background: rgba(255,255,255,0.03); }}"
                )

        detail_btn.clicked.connect(_toggle_detail)
        row.mousePressEvent = lambda event: _toggle_detail()

        return wrapper

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
        self._history_separator = sep
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
        """Build a compact single-row run history entry."""
        card = QFrame()
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setFixedHeight(36)
        card.setStyleSheet(
            "QFrame {"
            "  background: rgba(255,255,255,0.04); border-radius: 4px;"
            "  padding: 4px 10px; margin: 1px 10px;"
            "}"
            "QFrame:hover { background: rgba(255,255,255,0.08); }"
        )
        layout = QHBoxLayout(card)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Status icon
        status = run_data.get("status", "unknown")
        if status == "completed":
            icon_text = "\u2713"
            icon_color = SUCCESS
        elif status == "failed":
            icon_text = "\u2717"
            icon_color = DANGER
        else:
            icon_text = "\u25D0"
            icon_color = WARN

        icon = QLabel(icon_text)
        icon.setStyleSheet(f"color: {icon_color}; font-size: 14px; font-weight: bold; background: transparent;")
        icon.setFixedWidth(20)
        layout.addWidget(icon)

        # Drone name
        name_label = QLabel(run_data.get("drone_name", "Unknown"))
        name_label.setStyleSheet(f"color: {FG}; font-size: 12px; font-weight: 600; background: transparent;")
        name_label.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(name_label)

        # Status badge
        if status == "completed":
            badge_color = SUCCESS
            badge_text = "completed"
        elif status == "failed":
            badge_color = DANGER
            badge_text = "failed"
        else:
            badge_color = WARN
            badge_text = status if status else "running"

        status_badge = QLabel(badge_text)
        status_badge.setStyleSheet(
            f"font-size: 10px; color: {badge_color}; background: transparent; "
            f"padding: 1px 6px; border: 1px solid {badge_color}; border-radius: 3px;"
        )
        layout.addWidget(status_badge)

        # Duration
        elapsed = run_data.get("elapsed_seconds", 0)
        if elapsed < 60:
            dur_str = f"{elapsed:.0f}s"
        else:
            dur_str = f"{elapsed/60:.1f}m"
        dur_label = QLabel(dur_str)
        dur_label.setStyleSheet(f"font-size: 11px; color: {FG_MUTED}; background: transparent;")
        layout.addWidget(dur_label)

        # Tool count
        tool_count = len(run_data.get("tool_calls", []))
        calls_label = QLabel(f"{tool_count} calls")
        calls_label.setStyleSheet(f"font-size: 11px; color: {FG_MUTED}; background: transparent;")
        layout.addWidget(calls_label)

        layout.addStretch()

        # Timestamp
        started_at = run_data.get("started_at", "")
        try:
            import datetime
            ts = datetime.datetime.fromisoformat(started_at)
            time_str = ts.strftime("%Y-%m-%d %H:%M")
        except Exception:
            time_str = started_at[:16] if started_at else "?"
        ts_label = QLabel(time_str)
        ts_label.setStyleSheet(f"font-size: 11px; color: {FG_MUTED}; background: transparent;")
        layout.addWidget(ts_label)

        # View button
        view_btn = QPushButton("View")
        view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        view_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {ACCENT}; "
            f"border: 1px solid {BORDER}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 10px; }}"
            f"QPushButton:hover {{ border-color: {ACCENT}; }}"
        )
        run_id = run_data.get("run_id", "")
        view_btn.clicked.connect(
            lambda checked=False, rid=run_id: self.viewRunReceiptRequested.emit(rid)
        )
        layout.addWidget(view_btn)

        # Make clickable
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
        if self._history_separator is not None:
            self._card_layout.removeWidget(self._history_separator)
            self._history_separator.deleteLater()
            self._history_separator = None
        self._run_history_widgets.clear()
