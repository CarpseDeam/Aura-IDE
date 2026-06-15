from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QIcon, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from aura.config import media_path
from aura.drones.chain import ChainDefinition
from aura.drones.chain_runner import get_last_chain_run
from aura.drones.chain_store import _chain_from_dict, list_chains, load_chain, save_chain
from aura.drones.definition import DroneDefinition
from aura.drones.store import DroneStore
from aura.gui.drones.chain_canvas import (
    ChainCanvas,
    ChainEdgeItem,
    ChainNodeItem,
    GoalPlanetItem,
    MissionCoreItem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Galaxy palette
# ---------------------------------------------------------------------------
BG = "#141418"
SURFACE = "#1c1c22"
SURFACE_RAISED = "#222228"
BORDER = "#252830"
FG = "#eaecef"
FG_MUTED = "#6e7382"
FG_DIM = "#a8aebb"
ACCENT = "#c4b5fd"
ACCENT_DIM = "#7c6bb0"
DRONE_PURPLE = "#8b5cf6"
SEPARATOR = "#252830"

def _qss_color(hex_str: str) -> str:
    """Return a QColor string suitable for inline stylesheet use."""
    c = QColor(hex_str)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha() / 255:.2f})"

def _read_cargo_for_chain(workspace_root: Path, chain_id: str) -> tuple[list[dict], str]:
    """Read the latest ChainRun node outputs as cargo items.

    Returns (cargo_items, run_status).
    """
    if not chain_id:
        return [], "idle"

    chain_run = get_last_chain_run(workspace_root, chain_id)
    if chain_run is None:
        return [], "idle"

    cargo_items: list[dict] = []
    for node_run in chain_run.node_runs.values():
        if node_run.get("status") != "completed":
            continue

        artifact_path = node_run.get("artifact_path", "")
        drone_id = node_run.get("drone_id", "?")

        label = f"Output from {drone_id}"
        if artifact_path:
            output_path = workspace_root / artifact_path
            if output_path.exists():
                try:
                    data = json.loads(output_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {"artifact": data}
                    label = data.get("summary", label)
                except (json.JSONDecodeError, OSError):
                    label = "(unreadable output)"
            else:
                label = "(missing output)"

        cargo_items.append({
            "node_id": node_run.get("node_id", ""),
            "drone_id": drone_id,
            "status": node_run.get("status", "unknown"),
            "artifact_path": artifact_path,
            "met": node_run.get("met", False),
            "error": node_run.get("error", ""),
            "label": label,
        })

    return cargo_items, chain_run.status


# ---------------------------------------------------------------------------
# DroneCard – roster item
# ---------------------------------------------------------------------------
class _DroneCard(QFrame):
    """A single drone card in the roster with drag initiation."""

    def __init__(
        self,
        drone_id: str,
        name: str,
        description: str,
        write_policy: str = "read_only",
        status: str = "Ready",
        ready: bool = True,
        folder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.drone_id = drone_id
        self.drone_name = name
        self.folder = folder
        self._write_policy = write_policy
        self._ready = ready
        self.setObjectName("drone_card")
        self.setStyleSheet(
            "#drone_card {"
            "  background: rgba(18, 20, 28, 0.90);"
            "  border: 1px solid rgba(255, 255, 255, 0.06);"
            "  border-radius: 10px;"
            "}"
            "#drone_card:hover {"
            "  border-color: rgba(196, 181, 253, 0.20);"
            "  background: rgba(24, 26, 36, 0.94);"
            "}"
        )
        self.setCursor(Qt.PointingHandCursor if ready else Qt.ArrowCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._drag_start_pos = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Title row: status dot + title
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)

        dot_color = "#7dcfff" if write_policy == "read_only" else "#e0af68"
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(
            f"background: {dot_color};"
            f"border-radius: 4px;"
            f"border: none;"
        )
        title_row.addWidget(dot)

        title = QLabel(name)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPixelSize(12)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {_qss_color(FG)}; background: transparent; border: none;")
        title_row.addWidget(title, 1)
        layout.addLayout(title_row)

        # Pill row: status, policy, and truncated description preview
        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 0, 0, 0)
        pill_row.setSpacing(6)

        status_color = {
            "Ready": "#9ece6a",
            "Needs Fix": "#f87171",
            "Building": "#e0af68",
            "Testing": "#e0af68",
            "Draft": "#6e7382",
        }.get(status, "#6e7382")
        status_pill = QLabel(status)
        status_pill.setStyleSheet(
            f"color: {status_color};"
            f"background: rgba(255, 255, 255, 0.05);"
            f"border: 1px solid rgba(255, 255, 255, 0.08);"
            f"border-radius: 3px;"
            f"padding: 1px 5px;"
            f"font-size: 10px;"
        )
        status_pill.setFixedHeight(16)
        pill_row.addWidget(status_pill)

        policy_text = "read-only" if write_policy == "read_only" else "writes"
        policy_color = "#7dcfff" if write_policy == "read_only" else "#e0af68"
        pill = QLabel(policy_text)
        pill.setStyleSheet(
            f"color: {policy_color};"
            f"background: rgba(255, 255, 255, 0.05);"
            f"border: 1px solid rgba(255, 255, 255, 0.08);"
            f"border-radius: 3px;"
            f"padding: 1px 5px;"
            f"font-size: 10px;"
        )
        pill.setFixedHeight(16)
        pill_row.addWidget(pill)

        preview_text = description if description else ""
        preview = QLabel(preview_text)
        preview.setStyleSheet(f"color: {_qss_color(FG_MUTED)}; font-size: 10px; background: transparent; border: none;")
        preview.setTextFormat(Qt.TextFormat.PlainText)
        preview.setWordWrap(False)
        preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        pill_row.addWidget(preview, 1)
        layout.addLayout(pill_row)

        # -- action buttons ------------------------------------------------
        self._on_run = lambda: None
        self._on_edit = lambda: None
        self._on_delete = lambda: None

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        # Run
        btn_run = QPushButton("\u25b6 Run")
        btn_run.setEnabled(ready)
        btn_run.setStyleSheet(
            f"QPushButton {{"
            f"  background: rgba(196, 181, 253, 0.12);"
            f"  border: 1px solid rgba(196, 181, 253, 0.18);"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(ACCENT)};"
            f"  padding: 2px 8px;"
            f"  font-size: 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(196, 181, 253, 0.22);"
            f"  border-color: rgba(196, 181, 253, 0.35);"
            f"  color: {_qss_color(FG)};"
            f"}}"
            f"QPushButton:disabled {{"
            f"  background: transparent;"
            f"  border-color: rgba(255, 255, 255, 0.05);"
            f"  color: {_qss_color(FG_MUTED)};"
            f"}}"
        )
        btn_run.setCursor(Qt.PointingHandCursor if ready else Qt.ArrowCursor)
        if ready:
            btn_run.clicked.connect(lambda checked, cb="_on_run": (getattr(self, cb, None) or (lambda: None))())
        btn_layout.addWidget(btn_run)

        # Edit
        btn_edit = QPushButton("\u270e Edit")
        btn_edit.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid rgba(255, 255, 255, 0.05);"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG_MUTED)};"
            f"  padding: 2px 8px;"
            f"  font-size: 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border-color: rgba(255, 255, 255, 0.15);"
            f"  color: {_qss_color(FG)};"
            f"}}"
        )
        btn_edit.setCursor(Qt.PointingHandCursor)
        btn_edit.clicked.connect(lambda checked, cb="_on_edit": (getattr(self, cb, None) or (lambda: None))())
        btn_layout.addWidget(btn_edit)

        # Delete
        btn_delete = QPushButton("\u2715 Delete")
        btn_delete.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid transparent;"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG_DIM)};"
            f"  padding: 2px 8px;"
            f"  font-size: 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  color: #f87171;"
            f"  border-color: rgba(248, 113, 113, 0.20);"
            f"}}"
        )
        btn_delete.setCursor(Qt.PointingHandCursor)
        btn_delete.clicked.connect(lambda checked, cb="_on_delete": (getattr(self, cb, None) or (lambda: None))())
        btn_layout.addWidget(btn_delete)

        layout.addLayout(btn_layout)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._ready:
            return
        if self._drag_start_pos is None:
            return
        if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < 10:
            return
        mime_data = QMimeData()
        mime_data.setText(self.drone_id)
        mime_data.setData("application/x-aura-drone-id", self.drone_id.encode())
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        pixmap = QPixmap(self.size())
        self.render(pixmap)
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.position().toPoint())
        self._drag_start_pos = None
        drag.exec(Qt.CopyAction)


# ---------------------------------------------------------------------------
# DroneRosterWidget – scrollable gallery of drone cards
# ---------------------------------------------------------------------------
class _DroneRosterWidget(QScrollArea):
    """Displays all available drones as cards in a vertical flow."""

    def __init__(self, workspace_root: Path, editor: ChainEditor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._editor = editor
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{ background: {_qss_color(BG)}; width: 6px; border-radius: 3px; }}"
            f"QScrollBar::handle:vertical {{ background: {_qss_color(BORDER)}; border-radius: 3px; min-height: 20px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
        )

        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)
        self._layout.addStretch()
        self.setWidget(self._content)

    def set_workspace_root(self, path: Path) -> None:
        self._workspace_root = path

    def populate(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        entries = DroneStore.list_drone_entries(self._workspace_root)
        if not entries:
            empty = QLabel("No Drones yet. Type /drone in chat to open Drone Builder.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {_qss_color(FG_MUTED)}; font-size: 11px; padding: 8px;")
            self._layout.insertWidget(self._layout.count() - 1, empty)
        for entry in entries:
            action_id = (
                f"builder:{entry.workspace_id}"
                if entry.workspace_id and not entry.ready
                else entry.id
            )
            card = _DroneCard(
                entry.id,
                entry.name,
                entry.description,
                entry.write_policy,
                entry.status,
                entry.ready,
                folder=entry.folder,
            )
            card._on_run = lambda did=entry.id: self._editor.runDroneRequested.emit(did)
            card._on_edit = lambda did=action_id: self._editor.editDroneRequested.emit(did)
            card._on_delete = lambda did=action_id: self._editor.deleteDroneRequested.emit(did)
            self._layout.insertWidget(self._layout.count() - 1, card)


# ---------------------------------------------------------------------------
# WorkflowTabBar – right-click context menu on tabs
# ---------------------------------------------------------------------------
class _WorkflowTabBar(QTabBar):
    tabContextMenuRequested = Signal(int)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.RightButton:
            index = self.tabAt(event.position().toPoint())
            self.tabContextMenuRequested.emit(index)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# ChainEditor – central workbay: tabbed workflows + drone roster + canvas
# ---------------------------------------------------------------------------
class ChainEditor(QWidget):
    """Workbay with tabbed workflows: drone roster (left) | canvas (center)."""

    closeRequested = Signal()
    runChainRequested = Signal(str)
    goBackRequested = Signal()
    runDroneRequested = Signal(str)
    editDroneRequested = Signal(str)
    deleteDroneRequested = Signal(str)

    _AUTO_SAVE_MS = 1200

    def __init__(
        self,
        workspace_root: Path,
        chain_id: str | None = None,
        provider_id: str = "deepseek",
        model: str = "",
        thinking: bool = False,
        temperature: float = 0.7,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._provider_id = provider_id
        self._model = model
        self._thinking = thinking
        self._temperature = temperature
        self._current_chain_id: str | None = None
        self._chain_id: str | None = None
        self._chain_name = ""
        self._chain_desc = ""
        self._auto_route = False
        self._dirty = False
        self._roster: _DroneRosterWidget | None = None

        # Tab state
        self._tabs: list[dict] = []
        self._current_tab_index: int | None = None

        self._build_layout()

        # Populate tabs from saved workflows
        self._tab_bar.blockSignals(True)
        chains = list_chains(self._workspace_root)
        if chains:
            target_idx = 0
            for i, chain_data in enumerate(chains):
                tab_data = {
                    "chain_id": chain_data["id"],
                    "name": chain_data.get("name", "Untitled"),
                    "description": chain_data.get("description", ""),
                    "auto_route": chain_data.get("auto_route", False),
                    "dirty": False,
                    "canvas_data": None,
                }
                self._tabs.append(tab_data)
                self._tab_bar.insertTab(i, tab_data["name"])
                if chain_id and chain_data["id"] == chain_id:
                    target_idx = i
            self._tab_bar.setCurrentIndex(target_idx)
            self._tab_bar.blockSignals(False)
            self._on_tab_changed(target_idx)
        else:
            tab_data = {
                "chain_id": None,
                "name": "Untitled",
                "description": "",
                "auto_route": False,
                "dirty": False,
                "canvas_data": None,
            }
            self._tabs.append(tab_data)
            self._tab_bar.insertTab(0, "Untitled")
            self._tab_bar.setCurrentIndex(0)
            self._tab_bar.blockSignals(False)
            self._on_tab_changed(0)

        self._roster.populate()

        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(self._AUTO_SAVE_MS)
        self._auto_save_timer.timeout.connect(self._save_chain)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- Tab bar row with Save/Load chrome ------------------------
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(0)

        self._tab_bar = _WorkflowTabBar()
        self._tab_bar.setExpanding(False)
        self._tab_bar.setDrawBase(True)
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        self._tab_bar.setTabsClosable(True)
        self._tab_bar.tabCloseRequested.connect(self._on_tab_close)
        self._tab_bar.tabContextMenuRequested.connect(self._on_tab_context_menu)
        self._tab_bar.setStyleSheet(
            f"QTabBar {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border-bottom: 1px solid {_qss_color(BORDER)};"
            f"}}"
            f"QTabBar::tab {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  color: {_qss_color(FG_MUTED)};"
            f"  padding: 6px 14px;"
            f"  height: 20px;"
            f"  border: none;"
            f"  border-bottom: 2px solid transparent;"
            f"  font-size: 12px;"
            f"}}"
            f"QTabBar::tab:selected {{"
            f"  background: {_qss_color(SURFACE_RAISED)};"
            f"  color: {_qss_color(FG)};"
            f"  border-bottom: 2px solid {_qss_color(ACCENT)};"
            f"}}"
            f"QTabBar::tab:hover:!selected {{"
            f"  background: rgba(255, 255, 255, 0.03);"
            f"}}"
            f"QTabBar::close-button {{"
            f"  width: 12px;"
            f"  height: 12px;"
            f"  subcontrol-position: right;"
            f"  padding: 0px;"
            f"}}"
            f"QTabBar::close-button:hover {{"
            f"  background: rgba(255, 255, 255, 0.12);"
            f"  border-radius: 3px;"
            f"}}"
        )
        # Add the "+" tab (always last)
        self._tab_bar.addTab("+")
        tab_row.addWidget(self._tab_bar, 1)

        # Save button
        self._save_btn = QPushButton("Save")
        self._save_btn.setIcon(QIcon(str(media_path("file_24.svg"))))
        self._save_btn.clicked.connect(self._save_chain)
        self._save_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid rgba(255, 255, 255, 0.06);"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG_MUTED)};"
            f"  font-size: 11px;"
            f"  padding: 2px 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border-color: rgba(255, 255, 255, 0.15);"
            f"  color: {_qss_color(FG)};"
            f"}}"
        )
        tab_row.addWidget(self._save_btn)

        # Load button
        self._load_btn = QPushButton("Load")
        self._load_btn.setIcon(QIcon(str(media_path("folder_24.svg"))))
        self._load_btn.clicked.connect(self._on_load_clicked)
        self._load_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid rgba(255, 255, 255, 0.06);"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG_MUTED)};"
            f"  font-size: 11px;"
            f"  padding: 2px 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border-color: rgba(255, 255, 255, 0.15);"
            f"  color: {_qss_color(FG)};"
            f"}}"
        )
        tab_row.addWidget(self._load_btn)


        # --- 2-pane splitter (roster | canvas) ------------------------
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setHandleWidth(2)
        self._splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {_qss_color(BORDER)}; }}"
        )
        root.addWidget(self._splitter, 1)

        # Left panel – drone roster
        self._build_left_panel()

        # Right pane: tab row above canvas
        right_pane = QWidget()
        right_pane.setStyleSheet(f"background: {_qss_color(BG)};")
        right_layout = QVBoxLayout(right_pane)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addLayout(tab_row)

        # Center – canvas
        self._canvas = ChainCanvas(self)
        self._canvas.setStyleSheet(f"background: {_qss_color(BG)}; border: none;")
        self._canvas.canvasChanged.connect(self._on_canvas_changed)
        self._canvas.runMissionRequested.connect(self._on_run_clicked)
        self._canvas.statusMessage.connect(self.set_status)
        self._canvas.renameWorkflowRequested.connect(self._rename_current_workflow)
        right_layout.addWidget(self._canvas, 1)

        self._splitter.addWidget(right_pane)

        self._splitter.setSizes([220, 580])

        self._hide_plus_close_button()

    def _build_left_panel(self) -> None:
        container = QWidget()
        container.setStyleSheet(f"background: {_qss_color(SURFACE)}; border-right: 1px solid {_qss_color(BORDER)};")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(0)

        # Header row
        header_row = QHBoxLayout()
        header_row.setContentsMargins(8, 2, 8, 4)
        drones_label = QLabel("Drones")
        drones_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {_qss_color(FG)}; padding: 0;")
        header_row.addWidget(drones_label)
        header_row.addStretch()
        layout.addLayout(header_row)

        # Roster
        self._roster = _DroneRosterWidget(self._workspace_root, self)
        layout.addWidget(self._roster, 1)

        self._splitter.addWidget(container)

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------
    def _on_tab_changed(self, index: int) -> None:
        num_tabs = len(self._tabs)

        # "+" tab clicked — create a new one
        if index == num_tabs:
            self._add_new_tab()
            return

        if index < 0 or index >= num_tabs:
            return

        # No-op if already on this tab
        if index == self._current_tab_index:
            return

        # Save current tab state before switching
        if self._current_tab_index is not None and self._current_tab_index < num_tabs:
            self._tabs[self._current_tab_index]["canvas_data"] = self._snapshot_chain()
            self._tabs[self._current_tab_index]["dirty"] = self._dirty
            self._tabs[self._current_tab_index]["name"] = self._chain_name
            self._tabs[self._current_tab_index]["description"] = self._chain_desc
            self._tabs[self._current_tab_index]["auto_route"] = self._auto_route

        # Switch to target tab
        self._current_tab_index = index
        tab = self._tabs[index]

        if tab["chain_id"]:
            self._load_or_create_chain(tab["chain_id"])
        else:
            self._load_or_create_chain(None, canvas_data=tab.get("canvas_data"))

        # Sync tab label with current name
        name = self._chain_name or "Untitled"
        suffix = " *" if self._dirty else ""
        self._tab_bar.setTabText(index, f"{name}{suffix}")

    def _add_new_tab(self) -> None:
        # Save current tab state
        if self._current_tab_index is not None and self._current_tab_index < len(self._tabs):
            self._tabs[self._current_tab_index]["canvas_data"] = self._snapshot_chain()
            self._tabs[self._current_tab_index]["dirty"] = self._dirty
            self._tabs[self._current_tab_index]["name"] = self._chain_name
            self._tabs[self._current_tab_index]["description"] = self._chain_desc
            self._tabs[self._current_tab_index]["auto_route"] = self._auto_route

        tab_data = {
            "chain_id": None,
            "name": "",
            "description": "",
            "auto_route": False,
            "dirty": False,
            "canvas_data": None,
        }
        self._tabs.append(tab_data)
        idx = len(self._tabs) - 1

        # Insert tab before the "+" tab
        self._tab_bar.insertTab(idx, "Untitled")
        self._tab_bar.setCurrentIndex(idx)
        self._current_tab_index = idx
        self._load_or_create_chain(None)
        self._hide_plus_close_button()

    # ------------------------------------------------------------------
    # Tab context menu
    # ------------------------------------------------------------------
    def _on_tab_context_menu(self, index: int) -> None:
        if index >= len(self._tabs):
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename Workflow")
        action = menu.exec(self._tab_bar.mapToGlobal(self._tab_bar.tabRect(index).bottomLeft()))
        if action == rename_action:
            self._rename_current_workflow()

    def _rename_current_workflow(self) -> None:
        text, ok = QInputDialog.getText(self, "Rename Workflow", "Workflow name:", text=self._chain_name)
        if ok and text.strip():
            self._chain_name = text.strip()
            self._save_chain()

    # ------------------------------------------------------------------
    # Chain loading / saving
    # ------------------------------------------------------------------
    def _load_or_create_chain(self, chain_id: str | None, canvas_data: dict | None = None) -> None:
        self._current_chain_id = chain_id
        if chain_id:
            data = load_chain(self._workspace_root, chain_id)
            if data:
                self._chain_id = chain_id
                self._chain_name = data.get("name", "")
                self._chain_desc = data.get("description", "")
                self._auto_route = data.get("auto_route", False)
                chain_def = _chain_from_dict(data)
                drone_lookup = self._build_drone_lookup()
                mission_core_data = data.get("mission_core")
                goal_planets_data = data.get("goals", [])
                self._canvas.load_chain(chain_def, drone_lookup, mission_core_data, goal_planets_data)
                self._dirty = False
                return
        if canvas_data:
            # Restore an unsaved tab from in-memory state
            self._chain_id = None
            self._chain_name = canvas_data.get("name", "")
            self._chain_desc = canvas_data.get("description", "")
            self._auto_route = canvas_data.get("auto_route", False)
            chain_def = _chain_from_dict(canvas_data)
            drone_lookup = self._build_drone_lookup()
            mission_core_data = canvas_data.get("mission_core")
            goal_planets_data = canvas_data.get("goals", [])
            self._canvas.load_chain(chain_def, drone_lookup, mission_core_data, goal_planets_data)
            self._dirty = canvas_data.get("dirty", False)
            return
        # New blank chain
        self._chain_id = None
        self._chain_name = ""
        self._chain_desc = ""
        self._auto_route = False
        chain_def = ChainDefinition(id="", name="", description="", nodes=(), edges=())
        self._canvas.load_chain(chain_def, self._build_drone_lookup())
        self._dirty = False
        self._current_chain_id = None

    def _build_drone_lookup(self) -> dict[str, DroneDefinition]:
        return {d.id: d for d in DroneStore.list_drones(self._workspace_root)}

    def _snapshot_chain(self) -> dict:
        nodes, edges, mission_core, goals = self._canvas.to_chain_nodes_and_edges()
        result = {
            "nodes": nodes,
            "edges": edges,
            "name": self._chain_name,
            "description": self._chain_desc,
            "auto_route": self._auto_route,
            "goals": goals,
        }
        if mission_core:
            result["mission_core"] = mission_core
        return result

    def _hide_plus_close_button(self) -> None:
        """Remove the close button from the trailing "+" tab."""
        plus_idx = len(self._tabs)
        self._tab_bar.setTabButton(plus_idx, QTabBar.ButtonPosition.RightSide, None)

    def _on_tab_close(self, index: int) -> None:
        """Handle close-request for a tab at *index*.  Does NOT touch disk."""
        num_tabs = len(self._tabs)

        # Ignore clicks on the "+" tab
        if index == num_tabs:
            return
        if index < 0 or index >= num_tabs:
            return

        # If closing a tab different from the current one, switch to it first
        # so the canvas shows its content and _prompt_save_changes sees the right chain.
        if index != self._current_tab_index:
            self._tab_bar.setCurrentIndex(index)
            if self._current_tab_index != index:
                return

        # Snapshot current canvas state into the tab dict
        self._tabs[index]["canvas_data"] = self._snapshot_chain()
        self._tabs[index]["dirty"] = self._dirty
        self._tabs[index]["name"] = self._chain_name
        self._tabs[index]["description"] = self._chain_desc
        self._tabs[index]["auto_route"] = self._auto_route

        # Prompt save if dirty
        if self._tabs[index].get("dirty", False):
            choice = self._prompt_save_changes()
            if choice == "cancel":
                return  # Tab stays open

        # Remove the tab
        self._tab_bar.blockSignals(True)
        self._tab_bar.removeTab(index)
        self._tab_bar.blockSignals(False)
        del self._tabs[index]

        # Adjust current tab index
        if self._current_tab_index is not None and self._current_tab_index > index:
            self._current_tab_index -= 1
        elif self._current_tab_index == index:
            self._current_tab_index = None

        # Handle empty state or lost current
        if len(self._tabs) == 0:
            tab_data = {
                "chain_id": None,
                "name": "",
                "description": "",
                "auto_route": False,
                "dirty": False,
                "canvas_data": None,
            }
            self._tabs.append(tab_data)
            self._tab_bar.blockSignals(True)
            self._tab_bar.insertTab(0, "Untitled")
            self._tab_bar.blockSignals(False)
            self._current_tab_index = 0
            self._load_or_create_chain(None)
        elif self._current_tab_index is None:
            self._tab_bar.blockSignals(True)
            self._tab_bar.setCurrentIndex(0)
            self._tab_bar.blockSignals(False)
            # _on_tab_changed fires from setCurrentIndex

        self._hide_plus_close_button()

    def _on_load_clicked(self) -> None:
        """Show a dialog listing saved workflows and open the selected one."""
        chains = list_chains(self._workspace_root)

        # Filter out workflows already open in a tab
        open_ids = {t["chain_id"] for t in self._tabs if t["chain_id"]}
        available = [c for c in chains if c.get("id") not in open_ids]

        if not available:
            QMessageBox.information(self, "Load Workflow", "All saved workflows are already open.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Load Workflow")
        dialog.setMinimumWidth(320)
        dialog.setStyleSheet(
            f"QDialog {{ background: {_qss_color(SURFACE)}; }}"
            f"QLabel {{ color: {_qss_color(FG)}; font-size: 13px; }}"
            f"QListWidget {{ background: {_qss_color(SURFACE_RAISED)}; color: {_qss_color(FG)}; border: 1px solid {_qss_color(BORDER)}; }}"
            f"QPushButton {{ background: {_qss_color(SURFACE_RAISED)}; color: {_qss_color(FG)}; border: 1px solid {_qss_color(BORDER)}; border-radius: 4px; padding: 4px 14px; }}"
            f"QPushButton:hover {{ border-color: {_qss_color(ACCENT_DIM)}; }}"
        )
        layout = QVBoxLayout(dialog)
        layout.setSpacing(8)

        label = QLabel("Select a workflow to open:")
        layout.addWidget(label)

        list_widget = QListWidget()
        for c in available:
            list_widget.addItem(c.get("name", "Untitled"))
        layout.addWidget(list_widget)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        cancel_btn = QPushButton("Cancel")
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        def _on_accept():
            row = list_widget.currentRow()
            if row < 0:
                return
            selected = available[row]
            selected_id = selected["id"]
            # Check again if it somehow got opened while dialog was up
            for i, t in enumerate(self._tabs):
                if t["chain_id"] == selected_id:
                    self._tab_bar.setCurrentIndex(i)
                    dialog.accept()
                    return
            # New tab
            tab_data = {
                "chain_id": selected_id,
                "name": selected.get("name", "Untitled"),
                "description": selected.get("description", ""),
                "auto_route": selected.get("auto_route", False),
                "dirty": False,
                "canvas_data": None,
            }
            self._tabs.append(tab_data)
            idx = len(self._tabs) - 1
            self._tab_bar.blockSignals(True)
            self._tab_bar.insertTab(idx, tab_data["name"])
            self._tab_bar.blockSignals(False)
            self._tab_bar.setCurrentIndex(idx)
            self._current_tab_index = idx
            self._load_or_create_chain(selected_id)
            self._hide_plus_close_button()
            dialog.accept()

        ok_btn.clicked.connect(_on_accept)
        cancel_btn.clicked.connect(dialog.reject)
        list_widget.itemDoubleClicked.connect(lambda: _on_accept())

        dialog.exec()

    def _save_chain(self) -> None:
        data = self._snapshot_chain()

        if self._chain_id:
            save_chain(self._workspace_root, self._chain_id, data)
            self._current_chain_id = self._chain_id
        else:
            chain_id = save_chain(self._workspace_root, None, data)
            self._chain_id = chain_id
            self._current_chain_id = chain_id
        self._dirty = False
        self._update_tab_label()
        self.set_status("Workflow saved.", "ok")

    def _update_tab_label(self) -> None:
        if self._current_tab_index is None or self._current_tab_index >= len(self._tabs):
            return
        name = self._chain_name or "Untitled"
        suffix = " *" if self._dirty else ""
        full = f"{name}{suffix}"
        self._tab_bar.setTabText(self._current_tab_index, full)
        if self._current_tab_index < len(self._tabs):
            self._tabs[self._current_tab_index]["name"] = self._chain_name
            self._tabs[self._current_tab_index]["chain_id"] = self._chain_id

    def _prompt_save_changes(self) -> str:
        """Ask user whether to Save, Discard, or Cancel. Returns 'save', 'discard', or 'cancel'."""
        result = QMessageBox.question(
            self,
            "Unsaved Changes",
            "Save changes to current workflow?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if result == QMessageBox.Save:
            self._save_chain()
            return "save"
        elif result == QMessageBox.Discard:
            return "discard"
        return "cancel"

    def set_status(self, text: str, level: str = "info") -> None:
        """Display a status message (can be connected to main window status bar)."""
        logger.info(f"ChainEditor status [{level}]: {text}")

    # ------------------------------------------------------------------
    # Workspace root
    # ------------------------------------------------------------------
    def set_workspace_root(self, path: Path) -> None:
        self._workspace_root = path
        self._roster.set_workspace_root(path)
        self._load_or_create_chain(self._chain_id)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    # ------------------------------------------------------------------
    # Canvas event handlers
    # ------------------------------------------------------------------
    def _on_canvas_changed(self) -> None:
        self._dirty = True
        self._auto_save_timer.start()
        self._update_tab_label()

    # ------------------------------------------------------------------
    # Run / Delete
    # ------------------------------------------------------------------
    def _on_run_clicked(self) -> None:
        if self._dirty:
            self._save_chain()
        if not self._current_chain_id:
            QMessageBox.warning(self, "Cannot Run", "Save the workflow first.")
            return
        data = load_chain(self._workspace_root, self._current_chain_id)
        if not data:
            QMessageBox.warning(self, "Cannot Run", "Workflow data not found.")
            return
        self.runChainRequested.emit(self._current_chain_id)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def refresh_roster(self) -> None:
        """Refresh the drone roster after external edit/delete. Called by main window."""
        if self._roster is not None:
            self._roster.set_workspace_root(self._workspace_root)
            self._roster.populate()
        if self._canvas._nodes or self._canvas._mission_core is not None or self._canvas._goal_planets:
            data = self._snapshot_chain()
            chain_def = _chain_from_dict(data)
            drone_lookup = self._build_drone_lookup()
            mission_core_data = data.get("mission_core")
            goal_planet_data = data.get("goals", [])
            self._canvas.load_chain(chain_def, drone_lookup, mission_core_data, goal_planet_data)

    def refresh_run_state(self) -> None:
        """Refresh mission core stats and assignment run status after a chain run."""
        chain_id = self._current_chain_id
        if not chain_id:
            return

        cargo_items, run_status = _read_cargo_for_chain(self._workspace_root, chain_id)

        # Update MissionCoreItem if present
        mc = self._canvas._mission_core
        if mc is not None:
            mc._cargo_count = len(cargo_items)
            mc._output_status = run_status
            mc.update()

        # Update assignment node run status from the latest ChainRun
        chain_run = get_last_chain_run(self._workspace_root, chain_id)
        if chain_run is not None:
            node_runs = chain_run.node_runs
            for node in self._canvas._nodes.values():
                if not node.is_assignment:
                    continue
                nr = node_runs.get(node.node_id)
                node.run_status = nr.get("status", "idle") if nr else "idle"

    def chain_editor(self) -> ChainEditor:
        """Return self for compatibility with popout window accessor."""
        return self
