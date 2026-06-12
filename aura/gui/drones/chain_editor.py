from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QPainter, QPixmap, QIcon, QEnterEvent, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.drones.build_spec import BuildMode, BuildSpec
from aura.gui.drones.chain_canvas import (
    ChainCanvas,
    ChainEdgeItem,
    ChainNodeItem,
)
from aura.drones.chain_store import load_chain, save_chain, delete_chain, list_chains
from aura.drones.drone_registry import DroneRegistry
from aura.gui.drones.drone_workshop_panel import DroneWorkshopPanel
from aura.gui.drones.workflow_list_pane import WorkflowListPane

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Galaxy palette
# ---------------------------------------------------------------------------
BG = "#0d0d12"
SURFACE = "#14141c"
SURFACE_RAISED = "#1a1a25"
BORDER = "#282838"
FG = "#e0ddf0"
FG_MUTED = "#8f8ca0"
FG_DIM = "#5f5d70"
ACCENT = "#c4b5fd"
ACCENT_DIM = "#7c6bb0"
DRONE_PURPLE = "#8b5cf6"
SEPARATOR = "#282838"


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


# ---------------------------------------------------------------------------
# DroneCard – roster item
# ---------------------------------------------------------------------------
class _DroneCard(QFrame):
    """A single drone card in the roster with drag initiation."""

    def __init__(self, drone_id: str, name: str, description: str, accepts: str, produces: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.drone_id = drone_id
        self.drone_name = name
        self.accepts = accepts
        self.produces = produces
        self.setObjectName("drone_card")
        self.setStyleSheet(
            f"#drone_card {{"
            f"  background: {_qss_color(SURFACE_RAISED)};"
            f"  border: 1px solid {_qss_color(BORDER)};"
            f"  border-radius: 6px;"
            f"  padding: 6px;"
            f"}}"
            f"#drone_card:hover {{"
            f"  border-color: {_qss_color(ACCENT_DIM)};"
            f"  background: {_qss_darker(ACCENT_DIM, 0.15)};"
            f"}}"
        )
        self.setCursor(Qt.PointingHandCursor)
        self._drag_start_pos = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        title = QLabel(name)
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPixelSize(12)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {_qss_color(FG)}; background: transparent; border: none;")
        layout.addWidget(title)

        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            desc.setMaximumHeight(32)
            desc.setStyleSheet(f"color: {_qss_color(FG_MUTED)}; font-size: 11px; background: transparent; border: none;")
            layout.addWidget(desc)

        meta = QLabel(f"In: {accepts}  ·  Out: {produces}")
        meta.setStyleSheet(f"color: {_qss_color(FG_DIM)}; font-size: 10px; background: transparent; border: none;")
        layout.addWidget(meta)

        # -- action buttons ------------------------------------------------
        self._on_run = lambda: None
        self._on_edit = lambda: None
        self._on_delete = lambda: None

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)
        btn_style = (
            f"QPushButton {{ background: {_qss_color(SURFACE)}; color: {_qss_color(FG_MUTED)}; "
            f"border: 1px solid {_qss_color(BORDER)}; border-radius: 4px; padding: 2px 6px; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {_qss_color(FG)}; border-color: {_qss_color(ACCENT_DIM)}; }}"
        )
        for text, callback_attr in [("▶ Run", "_on_run"), ("✎ Edit", "_on_edit"), ("✕ Delete", "_on_delete")]:
            btn = QPushButton(text)
            btn.setStyleSheet(btn_style)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, cb=callback_attr: (getattr(self, cb, None) or (lambda: None))())
            btn_layout.addWidget(btn)
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
        drag = QDrag(self)
        mime_data = drag.mimeData()
        mime_data.setText(self.drone_id)
        mime_data.setData("application/x-auradrone", self.drone_id.encode())
        pixmap = QPixmap(self.size())
        self.render(pixmap)
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.position().toPoint())
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
        registry = DroneRegistry(self._workspace_root)
        drones = registry.list_drones()
        if not drones:
            empty = QLabel("No drones saved yet.\nClick + New Drone to build one.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {_qss_color(FG_MUTED)}; font-size: 11px; padding: 8px;")
            self._layout.insertWidget(self._layout.count() - 1, empty)
        for d in drones:
            card = _DroneCard(d["id"], d["name"], d.get("description", ""), d.get("accepts", "any"), d.get("produces", "any"))
            card._on_run = lambda did=d["id"]: self._editor.runDroneRequested.emit(did)
            card._on_edit = lambda did=d["id"]: self._editor.editDroneRequested.emit(did)
            card._on_delete = lambda did=d["id"]: self._editor.deleteDroneRequested.emit(did)
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

    # -- rebuild ----------------------------------------------------------
    def rebuild(self) -> None:
        self._clear()
        items = self._editor._canvas._scene.selectedItems()
        nodes = [i for i in items if isinstance(i, ChainNodeItem)]
        edges = [i for i in items if isinstance(i, ChainEdgeItem)]

        if nodes:
            self._rebuild_node_form(nodes[0])
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
    settle_draft_requested = Signal(dict)
    runDroneRequested = Signal(str)
    editDroneRequested = Signal(str)
    deleteDroneRequested = Signal(str)

    _AUTO_SAVE_MS = 1200

    def __init__(
        self,
        workspace_root: Path,
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
        self._workshop_draft_node_id: str | None = None
        self._workshop_container: QWidget | None = None
        self._workshop_panel: DroneWorkshopPanel | None = None
        self._ws_draft_label: QLabel | None = None
        self._ws_contracts_label: QLabel | None = None
        self._roster: _DroneRosterWidget | None = None
        self._property_panel: _PropertyPanel | None = None
        self._right_stack: QStackedWidget | None = None

        self._build_layout()
        self._load_or_create_chain(None)
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

        # Header row: "Drones" + "+ New Drone"
        header_row = QHBoxLayout()
        header_row.setContentsMargins(8, 2, 8, 4)
        drones_label = QLabel("Drones")
        drones_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {_qss_color(FG)}; padding: 0;")
        header_row.addWidget(drones_label)
        header_row.addStretch()
        new_drone_btn = QPushButton("+ New Drone")
        new_drone_btn.setFlat(True)
        new_drone_btn.setCursor(Qt.PointingHandCursor)
        new_drone_btn.setStyleSheet(
            f"QPushButton {{ color: {_qss_color(ACCENT)}; font-size: 11px; font-weight: bold; padding: 2px 6px; border: none; }}"
            f"QPushButton:hover {{ color: {_qss_color(FG)}; }}"
        )
        new_drone_btn.clicked.connect(self._on_new_drone_clicked)
        header_row.addWidget(new_drone_btn)
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
        self._title_label = QLabel("Untitled Workflow")
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
        run_btn = QPushButton("Run")
        run_btn.setStyleSheet(
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
        run_btn.setCursor(Qt.PointingHandCursor)
        run_btn.clicked.connect(self._on_run_clicked)
        tb_layout.addWidget(run_btn)

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
                self._canvas.load_chain(data)
                self._dirty = False
                self._update_title_label()
                self._build_drone_lookup()
                self._property_panel.rebuild()
                self._update_context_panel()
                return
        # New chain
        self._chain_id = None
        self._chain_name = ""
        self._chain_desc = ""
        self._auto_route = False
        self._canvas.load_chain({"nodes": [], "edges": [], "name": "", "description": "", "auto_route": False})
        self._dirty = False
        self._current_chain_id = None
        self._update_title_label()
        self._property_panel.rebuild()

    def _build_drone_lookup(self) -> None:
        registry = DroneRegistry(self._workspace_root)
        drone_map = {d["id"]: d for d in registry.list_drones()}
        self._canvas.set_drone_lookup(drone_map)

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
        data = self._canvas.save_chain()
        data["name"] = self._chain_name
        data["description"] = self._chain_desc
        data["auto_route"] = self._auto_route
        return data

    def _save_chain(self) -> None:
        self._sync_chain_from_form()
        data = self._canvas.save_chain()
        data["name"] = self._chain_name
        data["description"] = self._chain_desc
        data["auto_route"] = self._auto_route

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
        data = self._canvas.save_chain()
        data["name"] = self._chain_name
        data["description"] = self._chain_desc
        data["auto_route"] = self._auto_route
        chain_id = save_chain(self._workspace_root, None, data)
        self._chain_id = chain_id
        self._current_chain_id = chain_id
        self._dirty = False
        self._update_title_label()
        self.set_status(f"Saved as {self._chain_name or chain_id}", "ok")

    def _validate_chain(self) -> None:
        self._sync_chain_from_form()
        data = self._canvas.save_chain()
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        issues: list[str] = []

        if not nodes:
            issues.append("No nodes in the workflow.")
        node_ids = {n["id"] for n in nodes}
        for e in edges:
            if e["source"] not in node_ids:
                issues.append(f"Edge references missing source node {e['source']}.")
            if e["dest"] not in node_ids:
                issues.append(f"Edge references missing destination node {e['dest']}.")

        has_draft = any(n.get("is_draft") for n in nodes)
        if has_draft:
            issues.append("One or more draft nodes have not been built yet.")

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
        name = self._chain_name or "Untitled Workflow"
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

        if not node_items and not edge_items:
            # Nothing selected: hide right panel
            sizes = self._splitter.sizes()
            if len(sizes) >= 3:
                self._splitter.setSizes([sizes[0], sizes[0] + sizes[1] + sizes[2], 0])
            return

        if node_items:
            node = node_items[0]
            if node.is_draft:
                self._ensure_workshop_panel()
                self._show_workshop_for_draft(node)
                self._right_stack.setCurrentIndex(1)
                w = max(300, self.width() - 500)
                self._splitter.setSizes([220, w, 280])
                return

        # Non-draft node or edge: show property panel
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

    # ------------------------------------------------------------------
    # Workshop panel (lazy, right panel page 1)
    # ------------------------------------------------------------------
    def _ensure_workshop_panel(self) -> QWidget:
        if self._workshop_container is not None:
            return self._workshop_container

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Back button
        back_btn = QPushButton("\u2190 Back")
        back_btn.setFlat(True)
        back_btn.setCursor(Qt.PointingHandCursor)
        back_btn.setStyleSheet(
            f"QPushButton {{ color: {_qss_color(FG_MUTED)}; text-align: left; padding: 2px; }}"
            f"QPushButton:hover {{ color: {_qss_color(ACCENT)}; }}"
        )
        back_btn.clicked.connect(self._on_back_to_palette)
        layout.addWidget(back_btn)

        # Workshop header
        ws_header = QLabel("Drone Workshop")
        ws_header.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {_qss_color(FG)}; padding: 2px 0;")
        layout.addWidget(ws_header)

        # Draft info header
        self._ws_draft_label = QLabel()
        self._ws_draft_label.setWordWrap(True)
        self._ws_draft_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(self._ws_draft_label)

        self._ws_contracts_label = QLabel()
        self._ws_contracts_label.setWordWrap(True)
        self._ws_contracts_label.setStyleSheet(f"color: {_qss_color(FG_MUTED)}; font-size: 11px;")
        layout.addWidget(self._ws_contracts_label)

        # Workshop panel
        self._workshop_panel = DroneWorkshopPanel(
            workspace_root=self._workspace_root,
            provider_id=self._provider_id,
            model=self._model,
            thinking=self._thinking,
            temperature=self._temperature,
            parent=self,
        )
        self._workshop_panel.drone_build_requested.connect(self._on_workshop_build_requested)
        self._workshop_panel.cancelled_and_back_requested.connect(self._on_back_to_palette)
        layout.addWidget(self._workshop_panel, 1)

        self._workshop_container = container
        self._right_stack.addWidget(container)  # page 1
        return container

    def _show_workshop_for_draft(self, draft_node: ChainNodeItem) -> None:
        if draft_node.node_id not in self._canvas._nodes:
            return
        self._ensure_workshop_panel()
        if draft_node.node_id != self._workshop_draft_node_id:
            self._workshop_panel.reset_workshop_state()
        name = draft_node.draft_name or "Untitled Drone"
        accepts = draft_node.draft_accepts or "any"
        produces = draft_node.draft_produces or "any"
        self._workshop_draft_node_id = draft_node.node_id
        self._ws_draft_label.setText(f"Building: {name}")
        self._ws_contracts_label.setText(f"In: {accepts}  ·  Out: {produces}")

    def _on_workshop_build_requested(self, spec: BuildSpec) -> None:
        """Workshop has produced a build spec — settle the draft node."""
        self.settle_draft_node(spec)

    def _on_back_to_palette(self) -> None:
        """Clear selection and hide right panel."""
        self._workshop_draft_node_id = None
        if self._workshop_panel is not None:
            self._workshop_panel.reset_workshop_state()
        self._canvas._scene.clearSelection()
        sizes = self._splitter.sizes()
        if len(sizes) >= 3:
            self._splitter.setSizes([sizes[0], sizes[0] + sizes[1] + sizes[2], 0])

    # ------------------------------------------------------------------
    # Drone roster interactions
    # ------------------------------------------------------------------
    def _on_new_drone_clicked(self) -> None:
        """Create a draft node on the canvas and open workshop in the right panel."""
        draft = self._canvas.create_draft_node("Untitled Drone", "any", "any")
        self._canvas._scene.clearSelection()
        draft.setSelected(True)
        self._on_selection_changed()

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
    # Settle draft node into a real drone
    # ------------------------------------------------------------------
    def settle_draft_node(self, spec: BuildSpec) -> None:
        draft_id = self._workshop_draft_node_id
        if draft_id is None or draft_id not in self._canvas._nodes:
            return
        node = self._canvas._nodes[draft_id]
        node.is_draft = False
        node._drone_id = spec.drone_id
        node._goal_template = spec.goal_template

        if spec.build_mode == BuildMode.EXISTING:
            registry = DroneRegistry(self._workspace_root)
            drone_data = registry.get_drone(spec.drone_id)
            if drone_data:
                node.drone = drone_data
                node.drone_name = drone_data["name"]
        else:
            from aura.drones.drone_registry import DroneRegistry
            registry = DroneRegistry(self._workspace_root)
            drone_data = registry.get_drone(spec.drone_id)
            if drone_data:
                node.drone = drone_data
                node.drone_name = drone_data["name"]

        node.update()
        self._workshop_draft_node_id = None
        self._canvas._scene.clearSelection()
        node.setSelected(True)
        self._roster.populate()
        self._on_selection_changed()
        self.set_status(f"Drone settled: {node.drone_name}", "ok")

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

    def chain_editor(self) -> ChainEditor:
        """Return self for compatibility with popout window accessor."""
        return self
