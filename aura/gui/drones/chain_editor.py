from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from aura.drones.chain import ChainDefinition, ChainGoal, ChainNode, ChainEdge, ChainValidation, validate
from aura.drones.chain_runner import get_last_chain_run
from aura.drones.chain_store import _chain_from_dict, delete_chain, load_chain, save_chain
from aura.drones.definition import DroneDefinition
from aura.drones.store import DroneStore
from aura.gui.drones.chain_canvas import (
    ChainCanvas,
    ChainEdgeItem,
    ChainNodeItem,
    GoalPlanetItem,
    MissionCoreItem,
)
from aura.gui.drones.workflow_list_pane import WorkflowListPane

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


def _widget_valid(w: object) -> bool:
    """Check a widget hasn't been C++ deleted (PySide6 sip.isdeleted)."""
    from PySide6 import sip
    return not sip.isdeleted(w)


def _qss_color(hex_str: str) -> str:
    """Return a QColor string suitable for inline stylesheet use."""
    c = QColor(hex_str)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha() / 255:.2f})"


def _qss_darker(hex_str: str, factor: float = 0.6) -> str:
    """Darken a hex color by factor (0-1)."""
    c = QColor(hex_str)
    return f"rgba({int(c.red() * factor)},{int(c.green() * factor)},{int(c.blue() * factor)},{c.alpha() / 255:.2f})"


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

    def __init__(self, drone_id: str, name: str, description: str, accepts: str, produces: str, write_policy: str = "read_only", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.drone_id = drone_id
        self.drone_name = name
        self.accepts = accepts
        self.produces = produces
        self._write_policy = write_policy
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
        self.setCursor(Qt.PointingHandCursor)
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

        # Pill row: status pill + truncated description preview
        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 0, 0, 0)
        pill_row.setSpacing(6)

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
        btn_run = QPushButton("▶ Run")
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
        )
        btn_run.setCursor(Qt.PointingHandCursor)
        btn_run.clicked.connect(lambda checked, cb="_on_run": (getattr(self, cb, None) or (lambda: None))())
        btn_layout.addWidget(btn_run)

        # Edit
        btn_edit = QPushButton("✎ Edit")
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
        btn_delete = QPushButton("✕ Delete")
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
        drones = DroneStore.list_drones(self._workspace_root)
        if not drones:
            empty = QLabel("No Drones installed. Type /drone in chat to open Drone Architect.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {_qss_color(FG_MUTED)}; font-size: 11px; padding: 8px;")
            self._layout.insertWidget(self._layout.count() - 1, empty)
        for d in drones:
            card = _DroneCard(d.id, d.name, d.description, d.accepts or "any", d.produces or "any", d.write_policy)
            card._on_run = lambda did=d.id: self._editor.runDroneRequested.emit(did)
            card._on_edit = lambda did=d.id: self._editor.editDroneRequested.emit(did)
            card._on_delete = lambda did=d.id: self._editor.deleteDroneRequested.emit(did)
            self._layout.insertWidget(self._layout.count() - 1, card)


# ---------------------------------------------------------------------------
# _PropertyPanel – shows chain/node/edge properties when selected
# ---------------------------------------------------------------------------
class _PropertyPanel(QScrollArea):
    """Scrollable form showing chain-level, node, or edge properties."""

    def __init__(self, editor: ChainEditor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editor = editor
        self._chain_name_input: QLineEdit | None = None
        self._chain_desc_input: QTextEdit | None = None
        self._auto_route_cb: QCheckBox | None = None
        self._current_mission_item: MissionCoreItem | None = None

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{ background: {_qss_color(BG)}; width: 6px; border-radius: 3px; }}"
            f"QScrollBar::handle:vertical {{ background: {_qss_color(BORDER)}; border-radius: 3px; min-height: 20px; }}"
        )
        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(6)
        self._layout.addStretch()
        self.setWidget(self._content)

    # -- helpers ----------------------------------------------------------
    def _clear(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chain_name_input = None
        self._chain_desc_input = None
        self._auto_route_cb = None

    def _add_label(self, text: str, bold: bool = False, color: str = FG) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        f = QFont()
        f.setPixelSize(12)
        if bold:
            f.setBold(True)
        lbl.setFont(f)
        lbl.setStyleSheet(f"color: {_qss_color(color)}; background: transparent; border: none;")
        self._layout.insertWidget(self._layout.count() - 1, lbl)
        return lbl

    def _add_separator(self) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background: {_qss_color(SEPARATOR)}; border: none; max-height: 1px;")
        self._layout.insertWidget(self._layout.count() - 1, sep)

    def _add_text_input(self, label: str, value: str, placeholder: str = "", changed_slot=None) -> QLineEdit:
        self._add_label(label, bold=False, color=FG_MUTED)
        inp = QLineEdit(value)
        inp.setPlaceholderText(placeholder)
        inp.setStyleSheet(
            f"QLineEdit {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 4px;"
            f"  padding: 4px 6px;"
            f"  color: {_qss_color(FG)};"
            f"}}"
            f"QLineEdit:focus {{ border-color: {_qss_color(ACCENT_DIM)}; }}"
        )
        if changed_slot:
            inp.textChanged.connect(changed_slot)
        self._layout.insertWidget(self._layout.count() - 1, inp)
        return inp

    def _add_text_edit(self, label: str, value: str, placeholder: str = "", changed_slot=None) -> QTextEdit:
        self._add_label(label, bold=False, color=FG_MUTED)
        te = QTextEdit(value)
        te.setPlaceholderText(placeholder)
        te.setMaximumHeight(80)
        te.setStyleSheet(
            f"QTextEdit {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 4px;"
            f"  padding: 4px 6px;"
            f"  color: {_qss_color(FG)};"
            f"}}"
            f"QTextEdit:focus {{ border-color: {_qss_color(ACCENT_DIM)}; }}"
        )
        if changed_slot:
            te.textChanged.connect(changed_slot)
        self._layout.insertWidget(self._layout.count() - 1, te)
        return te

    def _add_checkbox(self, label: str, checked: bool, toggled_slot=None) -> QCheckBox:
        cb = QCheckBox(label)
        cb.setChecked(checked)
        cb.setStyleSheet(
            f"QCheckBox {{ color: {_qss_color(FG)}; spacing: 6px; }}"
            f"QCheckBox::indicator {{ width: 14px; height: 14px; }}"
        )
        if toggled_slot:
            cb.toggled.connect(toggled_slot)
        self._layout.insertWidget(self._layout.count() - 1, cb)
        return cb

    def _rebuild_mission_core_form(self, mission_item: MissionCoreItem) -> None:
        self._current_mission_item = mission_item
        self._add_label("Mission Core", bold=True)
        self._add_separator()

        name_input = self._add_text_input("Name", mission_item.title, "Mission Core name")
        name_input.textChanged.connect(
            lambda text, item=mission_item: self._on_mission_core_name_changed(item, text)
        )

        self._add_label(f"Launch Queue: {len(mission_item.assigned_drone_ids)} drones", color=FG_DIM)

        cargo_items, run_status = _read_cargo_for_chain(
            self._editor._workspace_root, self._editor._current_chain_id or ""
        )
        mission_item._cargo_count = len(cargo_items)
        mission_item._output_status = run_status
        mission_item.update()

        self._add_label(f"Cargo Bay: {len(cargo_items)} items", color=FG_DIM)
        _status_icons = {"completed": "\u2713", "running": "\u25ce", "failed": "\u2717", "idle": "\u25cb"}
        status_icon = _status_icons.get(run_status, "\u25cb")
        self._add_label(f"Status: {status_icon} {run_status}", color=FG_DIM)

        self._add_separator()
        self._add_label("Cargo Bay", bold=True)

        if not cargo_items:
            self._add_label("No cargo yet. Run the mission to collect output.", color=FG_MUTED)
        else:
            for cargo_item in cargo_items:
                self._add_cargo_row(cargo_item)

    def _on_mission_core_name_changed(self, item: MissionCoreItem, text: str) -> None:
        item.title = text
        self._editor._dirty = True
        self._editor._auto_save_timer.start()

    def _on_mission_core_goal_changed(self, item: MissionCoreItem, edit: QTextEdit) -> None:
        item.goal = edit.toPlainText()
        self._editor._dirty = True
        self._editor._auto_save_timer.start()

    def _rebuild_goal_planet_form(self, planet: GoalPlanetItem) -> None:
        heading = f"Goal Planet \u2014 {planet.title}" if planet.title else "Goal Planet"
        self._add_label(heading, bold=True)
        self._add_separator()

        self._add_label("Title", color=FG_MUTED)
        title_input = QLineEdit(planet.title)
        title_input.setPlaceholderText("Short title for this goal")
        title_input.setStyleSheet(
            f"QLineEdit {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG)};"
            f"  font-size: 11px;"
            f"  padding: 4px;"
            f"}}"
        )
        title_input.textChanged.connect(
            lambda text, p=planet, inp=title_input: self._on_goal_planet_title_changed(p, inp)
        )
        self._layout.insertWidget(self._layout.count() - 1, title_input)

        self._add_label("Mission Objective", color=FG_MUTED)
        goal_edit = QTextEdit()
        goal_edit.setPlainText(planet.objective)
        goal_edit.setPlaceholderText("Describe the mission goal\u2026")
        goal_edit.setMaximumHeight(120)
        goal_edit.setStyleSheet(
            f"QTextEdit {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG)};"
            f"  font-size: 11px;"
            f"  padding: 4px;"
            f"}}"
        )
        goal_edit.textChanged.connect(
            lambda: self._on_goal_planet_goal_changed(planet, goal_edit)
        )
        self._layout.insertWidget(self._layout.count() - 1, goal_edit)
        self._add_label("This is the mission objective.", color=FG_MUTED)

        btn_new_look = QPushButton("New Look")
        btn_new_look.setStyleSheet(
            f"QPushButton {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  color: {_qss_color(ACCENT)};"
            f"  padding: 4px 8px;"
            f"  border-radius: 3px;"
            f"  font-size: 11px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border-color: {_qss_color(ACCENT)};"
            f"}}"
        )
        btn_new_look.clicked.connect(
            lambda checked=False, p=planet: self._on_new_planet_look(p)
        )
        self._layout.insertWidget(self._layout.count() - 1, btn_new_look)

    def _on_goal_planet_goal_changed(self, planet: GoalPlanetItem, edit: QTextEdit) -> None:
        planet.objective = edit.toPlainText()
        self._editor._dirty = True
        self._editor._auto_save_timer.start()

    def _on_goal_planet_title_changed(self, planet: GoalPlanetItem, edit: QLineEdit) -> None:
        planet.title = edit.text()
        self._editor._dirty = True
        self._editor._auto_save_timer.start()

    def _on_new_planet_look(self, planet: GoalPlanetItem) -> None:
        planet.reroll_seed()
        self._editor._dirty = True
        self._editor._auto_save_timer.start()

    def _add_cargo_row(self, cargo_item: dict) -> None:
        label = cargo_item.get("label", f"Output from {cargo_item.get('drone_id', '?')}")
        elided = label[:40] + "\u2026" if len(label) > 40 else label
        drone_id = cargo_item.get("drone_id", "?")
        status = cargo_item.get("status", "unknown")
        status_dot = "\u25cf"
        text = f"{elided}  {status_dot}  {drone_id}"

        btn = QPushButton(text)
        btn.setFlat(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{"
            f"  text-align: left;"
            f"  color: {_qss_color(FG)};"
            f"  font-size: 10px;"
            f"  padding: 4px 6px;"
            f"  border: 1px solid rgba(255,255,255,0.06);"
            f"  border-radius: 4px;"
            f"  background: rgba(255,255,255,0.03);"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(255,255,255,0.06);"
            f"  border-color: rgba(196, 181, 253, 0.25);"
            f"}}"
        )
        btn.clicked.connect(lambda checked, item=cargo_item: self._show_cargo_detail(item))
        self._layout.insertWidget(self._layout.count() - 1, btn)

    def _show_cargo_detail(self, cargo_item: dict) -> None:
        self._clear()

        self._add_label("Cargo Detail", bold=True)
        self._add_separator()
        self._add_label(f"Source: {cargo_item.get('drone_id', '?')}")
        self._add_label(f"Status: {cargo_item.get('status', 'unknown')}")

        error = cargo_item.get("error", "")
        if error:
            self._add_label(f"Error: {error}", color="#f7768e")

        self._add_separator()

        artifact_path = cargo_item.get("artifact_path", "")
        if artifact_path:
            path = self._editor._workspace_root / artifact_path
            content = path.read_text(encoding="utf-8") if path.exists() else "(file missing)"
        else:
            content = "(no artifact path)"

        try:
            parsed = json.loads(content)
            content = json.dumps(parsed, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

        text_edit = QTextEdit(content)
        text_edit.setReadOnly(True)
        text_edit.setMaximumHeight(300)
        mono_font = QFont("Courier New")
        mono_font.setStyleHint(QFont.Monospace)
        mono_font.setPixelSize(11)
        text_edit.setFont(mono_font)
        text_edit.setStyleSheet(
            f"QTextEdit {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 4px;"
            f"  padding: 4px;"
            f"  color: {_qss_color(FG)};"
            f"}}"
        )
        self._layout.insertWidget(self._layout.count() - 1, text_edit)

        back_btn = QPushButton("\u2190 Back to Cargo Bay")
        back_btn.setFlat(True)
        back_btn.setCursor(Qt.PointingHandCursor)
        back_btn.setStyleSheet(
            f"QPushButton {{ color: {_qss_color(ACCENT)}; font-size: 11px; padding: 4px 6px; text-align: left; border: none; }}"
            f"QPushButton:hover {{ color: {_qss_color(FG)}; }}"
        )
        back_btn.clicked.connect(
            lambda: self._rebuild_mission_core_form(self._current_mission_item)
        )
        self._layout.insertWidget(self._layout.count() - 1, back_btn)

    # -- assignment form --------------------------------------------------
    def _rebuild_assignment_form(self, node: ChainNodeItem) -> None:
        self._add_label("Assignment", bold=True)
        self._add_separator()

        drone_name = node.drone.name if node.drone else "Unknown Drone"
        self._add_label(drone_name, bold=True)

        if node.drone and node.drone.description:
            self._add_label(node.drone.description, color=FG_MUTED)

        accepts = node.drone.accepts if node.drone and node.drone.accepts else "any"
        self._add_label(f"Takes: {accepts}", color=FG_DIM)

        produces = node.drone.produces if node.drone and node.drone.produces else "Summary"
        self._add_label(f"Brings back: {produces}", color=FG_DIM)

        policy = node.drone.write_policy if node.drone else "read_only"
        if policy == "read_only":
            self._add_label("Read Only", color="#7dcfff")
        else:
            self._add_label("Can Write", color="#e0af68")

        self._add_separator()

        # Target Goal Planet
        canvas = self._editor._canvas
        goals = canvas._goal_planets
        if node.goal_id and node.goal_id in goals:
            gp = goals[node.goal_id]
            display_title = gp.title or "Goal"
            snippet = gp.objective[:80] + "\u2026" if len(gp.objective) > 80 else gp.objective
            self._add_label(f"Target: {display_title}", bold=True)
            self._add_label(snippet, color=FG_MUTED)
            if len(goals) > 1:
                self._add_label("Change target:", color=FG_DIM)
                combo = QComboBox()
                ordered = list(goals.values())
                for gp_item in ordered:
                    label = gp_item.title or gp_item.goal_id or "Goal"
                    combo.addItem(label, gp_item.goal_id)
                current_idx = next((i for i, g in enumerate(ordered) if g.goal_id == node.goal_id), 0)
                combo.setCurrentIndex(current_idx)
                combo.currentIndexChanged.connect(
                    lambda idx, n=node, gps=ordered: self._on_assignment_goal_changed(n, gps[idx].goal_id)
                )
                combo.setStyleSheet(
                    f"QComboBox {{"
                    f"  background: {_qss_color(SURFACE)};"
                    f"  border: 1px solid {_qss_color(BORDER)};"
                    f"  border-radius: 4px;"
                    f"  color: {_qss_color(FG)};"
                    f"  padding: 3px 6px;"
                    f"  font-size: 10px;"
                    f"}}"
                )
                self._layout.insertWidget(self._layout.count() - 1, combo)
        elif not node.goal_id:
            self._add_label("Target: (not set)", bold=True, color="#e0af68")
            if goals:
                self._add_label("Change target:", color=FG_DIM)
                combo = QComboBox()
                combo.addItem("(none)", "")
                ordered = list(goals.values())
                for gp_item in ordered:
                    label = gp_item.title or gp_item.goal_id or "Goal"
                    combo.addItem(label, gp_item.goal_id)
                combo.currentIndexChanged.connect(
                    lambda idx, n=node, gps=[None] + ordered: self._on_assignment_goal_changed(
                        n, gps[idx].goal_id if gps[idx] else ""
                    )
                )
                combo.setStyleSheet(
                    f"QComboBox {{"
                    f"  background: {_qss_color(SURFACE)};"
                    f"  border: 1px solid {_qss_color(BORDER)};"
                    f"  border-radius: 4px;"
                    f"  color: {_qss_color(FG)};"
                    f"  padding: 3px 6px;"
                    f"  font-size: 10px;"
                    f"}}"
                )
                self._layout.insertWidget(self._layout.count() - 1, combo)

        self._add_separator()

        self._add_label("Task", color=FG_MUTED)
        task_edit = QTextEdit(node.goal_template)
        task_edit.setPlaceholderText("What should this drone do for this mission?")
        task_edit.setMaximumHeight(80)
        task_edit.setStyleSheet(
            f"QTextEdit {{"
            f"  background: {_qss_color(SURFACE)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 4px;"
            f"  padding: 4px 6px;"
            f"  color: {_qss_color(FG)};"
            f"}}"
            f"QTextEdit:focus {{ border-color: {_qss_color(ACCENT_DIM)}; }}"
        )
        task_edit.textChanged.connect(
            lambda: self._on_assignment_task_changed(node, task_edit)
        )
        self._layout.insertWidget(self._layout.count() - 1, task_edit)

        self._add_separator()
        delete_btn = QPushButton("Delete Assignment")
        delete_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid {_qss_color('#f7768e')};"
            f"  border-radius: 4px;"
            f"  color: {_qss_color('#f7768e')};"
            f"  padding: 4px 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(247, 118, 142, 0.10);"
            f"}}"
        )
        delete_btn.clicked.connect(lambda: self._on_delete_assignment(node))
        self._layout.insertWidget(self._layout.count() - 1, delete_btn)

    def _on_assignment_task_changed(self, node: ChainNodeItem, edit: QTextEdit) -> None:
        node.goal_template = edit.toPlainText()
        self._editor._dirty = True
        self._editor._auto_save_timer.start()

    def _on_assignment_goal_changed(self, node: ChainNodeItem, goal_id: str) -> None:
        node.goal_id = goal_id
        self._editor._dirty = True
        self._editor._auto_save_timer.start()
        # Refresh the panel to show updated target
        self._editor._property_panel.rebuild()

    def _on_delete_assignment(self, node: ChainNodeItem) -> None:
        self._editor._canvas._remove_node(node)
        self._editor._canvas._scene.clearSelection()
        self.rebuild()

    # -- rebuild ----------------------------------------------------------
    def rebuild(self) -> None:
        self._clear()
        items = self._editor._canvas._scene.selectedItems()
        mission_items = [i for i in items if isinstance(i, MissionCoreItem)]
        goal_planet_items = [i for i in items if isinstance(i, GoalPlanetItem)]
        nodes = [i for i in items if isinstance(i, ChainNodeItem)]
        edges = [i for i in items if isinstance(i, ChainEdgeItem)]

        if mission_items:
            self._rebuild_mission_core_form(mission_items[0])
        elif goal_planet_items:
            self._rebuild_goal_planet_form(goal_planet_items[0])
        elif nodes:
            node = nodes[0]
            if node.is_assignment:
                self._rebuild_assignment_form(node)
            else:
                self._rebuild_node_form(node)
        elif edges:
            self._rebuild_edge_form(edges[0])
        else:
            self._rebuild_chain_form()

    def _rebuild_chain_form(self) -> None:
        self._add_label("Workflow Properties", bold=True)
        self._add_separator()

        self._chain_name_input = self._add_text_input(
            "Name", self._editor._chain_name, "Workflow name",
            changed_slot=self._editor._on_chain_property_changed,
        )
        self._chain_desc_input = self._add_text_edit(
            "Description", self._editor._chain_desc, "Describe the workflow\u2026",
            changed_slot=self._editor._on_chain_property_changed,
        )
        self._auto_route_cb = self._add_checkbox(
            "Let the AI route between steps (auto-route)",
            getattr(self._editor, '_auto_route', False),
            toggled_slot=self._editor._on_chain_property_changed,
        )
        self._add_label(f"Chain ID: {self._editor._current_chain_id or '(unsaved)'}", color=FG_DIM)

    def _rebuild_node_form(self, node: ChainNodeItem) -> None:
        if node.is_assignment:
            self._rebuild_assignment_form(node)
            return
        self._add_label("Node Properties", bold=True)
        self._add_separator()
        name = node.drone.name if node.drone else "(missing)"
        if node.missing:
            name += " (missing)"
        self._add_label(name, bold=True)

        if node.drone:
            desc = node.drone.description
            if desc:
                self._add_label(str(desc), color=FG_MUTED)
            self._add_label(f"In: {node.drone.accepts}  ·  Out: {node.drone.produces}", color=FG_DIM)

        if node.goal_template:
            self._add_label("Goal Template", color=FG_MUTED)
            gt = QTextEdit(node.goal_template)
            gt.setReadOnly(True)
            gt.setMaximumHeight(60)
            gt.setStyleSheet(
                f"QTextEdit {{"
                f"  background: {_qss_color(SURFACE)};"
                f"  border: 1px solid {_qss_color(BORDER)};"
                f"  border-radius: 4px;"
                f"  padding: 4px;"
                f"  color: {_qss_color(FG)};"
                f"}}"
            )
            self._layout.insertWidget(self._layout.count() - 1, gt)

    def _rebuild_edge_form(self, edge: ChainEdgeItem) -> None:
        self._add_label("Connection", bold=True)
        self._add_separator()
        src_name = edge.source_node.drone.name if edge.source_node and edge.source_node.drone else "(unknown)"
        dst_name = edge.dest_node.drone.name if edge.dest_node and edge.dest_node.drone else "(unknown)"
        self._add_label(f"{src_name}  \u2192  {dst_name}", color=FG)


# ---------------------------------------------------------------------------
# ChainEditor – central workbay canvas + drone roster + contextual panel
# ---------------------------------------------------------------------------
class ChainEditor(QWidget):
    """Permanent 3-pane workbay: drone roster (left) | canvas (center) | details/workshop (right)."""

    titleChanged = Signal(str)
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
        self._property_panel: _PropertyPanel | None = None
        self._right_stack: QStackedWidget | None = None

        self._build_layout()
        self._load_or_create_chain(chain_id)
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

        # --- Toolbar (permanent workflow bar) -------------------------
        self._build_toolbar(root)

        # --- 3-pane splitter ------------------------------------------
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setHandleWidth(2)
        self._splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {_qss_color(BORDER)}; }}"
        )
        root.addWidget(self._splitter, 1)

        # Left panel – always drone roster
        self._build_left_panel()

        # Center – canvas
        self._canvas = ChainCanvas(self)
        self._canvas.setStyleSheet(f"background: {_qss_color(BG)}; border: none;")
        self._canvas.canvasChanged.connect(self._on_canvas_changed)
        self._canvas.runMissionRequested.connect(self._on_run_clicked)
        self._canvas.statusMessage.connect(self.set_status)
        self._canvas._scene.selectionChanged.connect(self._on_selection_changed)
        self._splitter.addWidget(self._canvas)

        # Right panel – contextual (details / workshop)
        self._build_right_panel()

        self._splitter.setSizes([220, 580, 0])

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

    def _build_right_panel(self) -> None:
        self._right_stack = QStackedWidget()
        self._right_stack.setStyleSheet(f"background: {_qss_color(SURFACE)}; border-left: 1px solid {_qss_color(BORDER)};")

        # --- Page 0: Property Panel (Details) -------------------------
        details_container = QWidget()
        details_layout = QVBoxLayout(details_container)
        details_layout.setContentsMargins(0, 4, 0, 0)
        details_layout.setSpacing(4)

        details_header_row = QHBoxLayout()
        details_header_row.setContentsMargins(8, 2, 8, 4)
        details_header = QLabel("Properties")
        details_header.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {_qss_color(FG_MUTED)}; padding: 0;")
        details_header_row.addWidget(details_header)
        details_header_row.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.setFlat(True)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(
            f"QPushButton {{ color: {_qss_color(FG_MUTED)}; font-size: 11px; padding: 2px 6px; border: none; }}"
            f"QPushButton:hover {{ color: {_qss_color(ACCENT)}; }}"
        )
        clear_btn.clicked.connect(self._on_clear_selection)
        details_header_row.addWidget(clear_btn)
        details_layout.addLayout(details_header_row)

        self._property_panel = _PropertyPanel(self)
        details_layout.addWidget(self._property_panel, 1)
        self._right_stack.addWidget(details_container)  # page 0

        self._splitter.addWidget(self._right_stack)

    def _build_toolbar(self, root_layout: QVBoxLayout) -> None:
        toolbar = QWidget()
        toolbar.setFixedHeight(36)
        toolbar.setStyleSheet(
            f"background: {_qss_color(SURFACE)};"
            f"border-bottom: 1px solid {_qss_color(BORDER)};"
        )
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 2, 8, 2)
        tb_layout.setSpacing(6)

        # Title label
        self._title_label = QLabel("Untitled Mission")
        self._title_label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {_qss_color(FG)}; padding-right: 10px; background: transparent; border: none;"
        )
        tb_layout.addWidget(self._title_label)
        tb_layout.addStretch()

        btn_style = (
            f"QPushButton {{"
            f"  background: {_qss_color(SURFACE_RAISED)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG)};"
            f"  padding: 2px 10px;"
            f"  font-size: 11px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border-color: {_qss_color(ACCENT_DIM)};"
            f"  color: {_qss_color(ACCENT)};"
            f"}}"
        )

        # New
        new_btn = QPushButton("New")
        new_btn.setStyleSheet(btn_style)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self._on_new_workflow_from_list)
        tb_layout.addWidget(new_btn)

        # Load
        load_btn = QPushButton("Load")
        load_btn.setStyleSheet(btn_style)
        load_btn.setCursor(Qt.PointingHandCursor)
        load_btn.clicked.connect(self._on_load_dialog)
        tb_layout.addWidget(load_btn)

        # Save
        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(btn_style)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self._save_chain)
        tb_layout.addWidget(save_btn)

        # Save As
        save_as_btn = QPushButton("Save As")
        save_as_btn.setStyleSheet(btn_style)
        save_as_btn.setCursor(Qt.PointingHandCursor)
        save_as_btn.clicked.connect(self._save_chain_as)
        tb_layout.addWidget(save_as_btn)

        # Validate
        validate_btn = QPushButton("Validate")
        validate_btn.setStyleSheet(btn_style)
        validate_btn.setCursor(Qt.PointingHandCursor)
        validate_btn.clicked.connect(self._validate_chain)
        tb_layout.addWidget(validate_btn)

        # Run
        self._run_btn = QPushButton("Run")
        self._run_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {_qss_color(ACCENT_DIM)};"
            f"  border: 1px solid {_qss_color(ACCENT_DIM)};"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG)};"
            f"  padding: 2px 10px;"
            f"  font-size: 11px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {_qss_color(ACCENT)};"
            f"}}"
        )
        self._run_btn.setCursor(Qt.PointingHandCursor)
        self._run_btn.clicked.connect(self._on_run_clicked)
        tb_layout.addWidget(self._run_btn)

        # Delete
        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid transparent;"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG_MUTED)};"
            f"  padding: 2px 8px;"
            f"  font-size: 11px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  color: #f87171;"
            f"}}"
        )
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.clicked.connect(self._on_delete_clicked)
        tb_layout.addWidget(delete_btn)

        # Close
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid transparent;"
            f"  border-radius: 4px;"
            f"  color: {_qss_color(FG_MUTED)};"
            f"  padding: 2px 8px;"
            f"  font-size: 11px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  color: #f87171;"
            f"}}"
        )
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.closeRequested.emit)
        tb_layout.addWidget(close_btn)

        root_layout.addWidget(toolbar)

    # ------------------------------------------------------------------
    # Chain loading / saving
    # ------------------------------------------------------------------
    def _load_or_create_chain(self, chain_id: str | None) -> None:
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
                self._update_title_label()
                self._property_panel.rebuild()
                self._update_context_panel()
                return
        # New chain
        self._chain_id = None
        self._chain_name = ""
        self._chain_desc = ""
        self._auto_route = False
        chain_def = ChainDefinition(id="", name="", description="", nodes=(), edges=())
        self._canvas.load_chain(chain_def, self._build_drone_lookup())
        self._dirty = False
        self._current_chain_id = None
        self._update_title_label()
        self._property_panel.rebuild()

    def _build_drone_lookup(self) -> dict[str, DroneDefinition]:
        return {d.id: d for d in DroneStore.list_drones(self._workspace_root)}

    def _sync_form_from_chain(self) -> None:
        if self._property_panel is None:
            return
        pp = self._property_panel
        if not _widget_valid(pp):
            return
        if pp._chain_name_input and _widget_valid(pp._chain_name_input):
            pp._chain_name_input.setText(self._chain_name)
        if pp._chain_desc_input and _widget_valid(pp._chain_desc_input):
            pp._chain_desc_input.setText(self._chain_desc)
        if pp._auto_route_cb and _widget_valid(pp._auto_route_cb):
            pp._auto_route_cb.setChecked(self._auto_route)

    def _sync_chain_from_form(self) -> None:
        pp = self._property_panel
        if pp is None or not _widget_valid(pp):
            return
        if pp._chain_name_input and _widget_valid(pp._chain_name_input):
            self._chain_name = pp._chain_name_input.text().strip()
        if pp._chain_desc_input and _widget_valid(pp._chain_desc_input):
            self._chain_desc = pp._chain_desc_input.toPlainText().strip()
        if pp._auto_route_cb and _widget_valid(pp._auto_route_cb):
            self._auto_route = pp._auto_route_cb.isChecked()

    def _snapshot_chain(self) -> dict:
        self._sync_chain_from_form()
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

    def _save_chain(self) -> None:
        self._sync_chain_from_form()
        data = self._snapshot_chain()

        if self._chain_id:
            save_chain(self._workspace_root, self._chain_id, data)
            self._current_chain_id = self._chain_id
        else:
            chain_id = save_chain(self._workspace_root, None, data)
            self._chain_id = chain_id
            self._current_chain_id = chain_id
        self._dirty = False
        self._update_title_label()
        self.set_status("Workflow saved.", "ok")

    def _save_chain_as(self) -> None:
        self._sync_chain_from_form()
        data = self._snapshot_chain()
        chain_id = save_chain(self._workspace_root, None, data)
        self._chain_id = chain_id
        self._current_chain_id = chain_id
        self._dirty = False
        self._update_title_label()
        self.set_status(f"Saved as {self._chain_name or chain_id}", "ok")

    def _validate_chain(self) -> None:
        self._sync_chain_from_form()
        nodes, edges, _mission_core, _goals = self._canvas.to_chain_nodes_and_edges()
        issues: list[str] = []

        # Shallow checks
        if not nodes:
            issues.append("No nodes in the workflow.")
        node_ids = {n["id"] for n in nodes}
        for e in edges:
            if e["from_node"] not in node_ids:
                issues.append(f"Edge references missing source node {e['from_node']}.")
            if e["to_node"] not in node_ids:
                issues.append(f"Edge references missing destination node {e['to_node']}.")

        has_draft = any(n.get("is_draft") for n in nodes)
        if has_draft:
            issues.append("One or more draft nodes have not been built yet.")

        # Build ChainDefinition and run shared validate()
        goal_planets_data = self._canvas.goal_planets_data
        chain_goals = [
            ChainGoal(
                id=g["id"],
                title=g.get("title", ""),
                objective=g.get("objective", ""),
                position=tuple(g.get("position", [0.0, 0.0])),
            )
            for g in goal_planets_data
        ]
        chain_nodes = [
            ChainNode(
                id=n["id"],
                drone_id=n["drone_id"],
                goal_template=n.get("goal_template", ""),
                position=tuple(n.get("position", [0.0, 0.0])),
                is_draft=n.get("is_draft", False),
                draft_name=n.get("draft_name", ""),
                draft_accepts=n.get("draft_accepts", ""),
                draft_produces=n.get("draft_produces", ""),
                draft_brief=n.get("draft_brief", ""),
                is_assignment=n.get("is_assignment", False),
                goal_id=n.get("goal_id", ""),
            )
            for n in nodes
        ]
        chain_edges = [
            ChainEdge(from_node=e["from_node"], to_node=e["to_node"])
            for e in edges
        ]
        chain_def = ChainDefinition(
            id=self._chain_id or "",
            name=self._chain_name,
            description=self._chain_desc,
            nodes=tuple(chain_nodes),
            edges=tuple(chain_edges),
            goals=tuple(chain_goals),
        )
        drone_lookup = self._build_drone_lookup()
        validation_result = validate(chain_def, drone_lookup)
        for err in validation_result.errors:
            issues.append(err)

        if issues:
            msg = "\n".join(f"• {i}" for i in issues)
            QMessageBox.warning(self, "Validation", f"Issues found:\n\n{msg}")
            self.set_status("Validation failed.", "error")
        else:
            QMessageBox.information(self, "Validation", "Workflow looks valid.")
            self.set_status("Validation passed.", "ok")

    def _prompt_save_changes(self) -> str | None:
        """Ask user whether to Save, Discard, or Cancel. Returns 'save', 'discard', or None (cancel)."""
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

    def _update_title_label(self) -> None:
        name = self._chain_name or "Untitled Mission"
        suffix = " *" if self._dirty else ""
        self._title_label.setText(f"\u25c6 {name}{suffix}")

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
        self._property_panel.rebuild()
        self._update_context_panel()
        self._update_title_label()
        if self._canvas._nodes:
            self._run_btn.setText("Run Mission")
        else:
            self._run_btn.setText("Run")

    def _on_chain_property_changed(self) -> None:
        self._dirty = True
        self._auto_save_timer.start()
        self._update_title_label()

    def _on_selection_changed(self) -> None:
        self._update_context_panel()

    # ------------------------------------------------------------------
    # Contextual right panel
    # ------------------------------------------------------------------
    def _update_context_panel(self) -> None:
        """Show/hide right panel based on canvas selection."""
        selection = self._canvas._scene.selectedItems()
        node_items = [i for i in selection if isinstance(i, ChainNodeItem)]
        edge_items = [i for i in selection if isinstance(i, ChainEdgeItem)]
        mission_items = [i for i in selection if isinstance(i, MissionCoreItem)]
        goal_planet_items = [i for i in selection if isinstance(i, GoalPlanetItem)]

        if not node_items and not edge_items and not mission_items and not goal_planet_items:
            # Nothing selected: hide right panel
            sizes = self._splitter.sizes()
            if len(sizes) >= 3:
                self._splitter.setSizes([sizes[0], sizes[0] + sizes[1] + sizes[2], 0])
            return

        if goal_planet_items:
            # Show property panel for goal planet
            self._right_stack.setCurrentIndex(0)
            self._property_panel.rebuild()
            w = max(300, self.width() - 500)
            self._splitter.setSizes([220, w, 280])
            return

        if mission_items:
            # Show property panel for mission core
            self._right_stack.setCurrentIndex(0)
            self._property_panel.rebuild()
            w = max(300, self.width() - 500)
            self._splitter.setSizes([220, w, 280])
            return

        # Node or edge: show property panel
        self._right_stack.setCurrentIndex(0)
        self._property_panel.rebuild()
        w = max(300, self.width() - 500)
        self._splitter.setSizes([220, w, 280])

    def _on_clear_selection(self) -> None:
        """Clear canvas selection and hide right panel."""
        self._canvas._scene.clearSelection()
        sizes = self._splitter.sizes()
        if len(sizes) >= 3:
            self._splitter.setSizes([sizes[0], sizes[0] + sizes[1] + sizes[2], 0])

    def _on_new_workflow_from_list(self) -> None:
        choice = self._prompt_save_changes()
        if choice == "cancel":
            return
        if choice == "save":
            self._save_chain()
        self._load_or_create_chain(None)

    def _on_load_existing_workflow(self, chain_id: str) -> None:
        choice = self._prompt_save_changes()
        if choice == "cancel":
            return
        if choice == "save":
            self._save_chain()
        self._load_or_create_chain(chain_id)

    def _on_delete_workflow_from_list(self, chain_id: str) -> None:
        delete_chain(self._workspace_root, chain_id)
        if self._current_chain_id == chain_id:
            self._load_or_create_chain(None)

    def _on_run_workflow_from_list(self, chain_id: str) -> None:
        self._load_or_create_chain(chain_id)
        self._on_run_clicked()

    # ------------------------------------------------------------------
    # Load dialog
    # ------------------------------------------------------------------
    def _on_load_dialog(self) -> None:
        if self._dirty and not self._prompt_save_changes():
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Load Workflow")
        dlg.resize(480, 400)
        layout = QVBoxLayout(dlg)
        wf_list = WorkflowListPane(self._workspace_root, dlg)
        layout.addWidget(wf_list)
        wf_list.set_workspace_root(self._workspace_root)

        result_chain_id: list[str | None] = [None]
        wf_list.editWorkflowRequested.connect(lambda cid: (result_chain_id.__setitem__(0, cid), dlg.accept()))  # type: ignore[func-returns-value]
        wf_list.newWorkflowRequested.connect(lambda: (self._on_new_workflow_from_list(), dlg.accept()))
        wf_list.deleteWorkflowRequested.connect(self._on_delete_workflow_from_list)
        wf_list.runWorkflowRequested.connect(lambda cid: (self._on_run_workflow_from_list(cid), dlg.accept()))

        if dlg.exec() == QDialog.DialogCode.Accepted and result_chain_id[0]:
            self._load_or_create_chain(result_chain_id[0])

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

    def _on_delete_clicked(self) -> None:
        if self._current_chain_id is None:
            QMessageBox.information(self, "Nothing to Delete", "No workflow is loaded.")
            return
        result = QMessageBox.question(
            self,
            "Delete Workflow",
            f"Permanently delete '{self._chain_name or self._current_chain_id}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if result == QMessageBox.Yes:
            delete_chain(self._workspace_root, self._current_chain_id)
            self._load_or_create_chain(None)

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

        # Refresh the property panel if mission core or assignment is selected
        self._property_panel.rebuild()

    def chain_editor(self) -> ChainEditor:
        """Return self for compatibility with popout window accessor."""
        return self
