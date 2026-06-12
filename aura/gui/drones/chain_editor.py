from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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

from aura.drones.chain import ChainDefinition, ChainEdge, ChainNode, validate
from aura.drones.chain_store import ChainStore
from aura.drones.contracts import BUILTIN_TYPES, is_compatible
from aura.drones.definition import DroneDefinition
from aura.drones.store import DroneStore
from aura.gui.drones.chain_canvas import ChainCanvas, ChainEdgeItem, ChainNodeItem
from aura.gui.drones.drone_workshop_panel import DroneWorkshopPanel
from aura.gui.drones.workflow_list_pane import WorkflowListPane
from aura.gui.theme import (
    ACCENT,
    BG,
    BG_ALT,
    BG_RAISED,
    BORDER,
    BORDER_STRONG,
    DANGER,
    FG,
    FG_DIM,
    FG_MUTED,
    SUCCESS,
    WARN,
)

logger = logging.getLogger(__name__)


def _widget_valid(w) -> bool:
    """Return True if the widget is not None and the underlying C++ object is alive."""
    if w is None:
        return False
    try:
        from shiboken6 import isValid
    except ImportError:
        return True  # best-effort if shiboken not available
    return isValid(w)


def _qss_color(color: QColor | str) -> str:
    """Return a CSS hex string from a QColor or plain string."""
    return color.name() if hasattr(color, "name") else str(color)


def _qss_darker(color: QColor | str, factor: int = 120) -> str:
    """Return a darkened CSS hex string from a QColor or plain string."""
    return QColor(str(color)).darker(factor).name()


class _DronePaletteList(QListWidget):
    """Left sidebar listing available drones with drag-to-canvas support."""

    def __init__(self, workspace_root: Path, parent=None):
        super().__init__(parent)
        self._workspace_root = workspace_root
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMinimumWidth(140)
        self.setMaximumWidth(220)
        self.setSpacing(2)
        self.setStyleSheet(f"""
            QListWidget {{
                background: {_qss_color(BG_RAISED)};
                border: none;
                padding: 4px;
                color: {_qss_color(FG)};
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-radius: 4px;
                border: 1px solid {_qss_color(BORDER)};
                margin: 2px 0;
                background: {_qss_color(BG_ALT)};
            }}
            QListWidget::item:hover {{
                background: {_qss_color(BG)};
                border: 1px solid {_qss_color(BORDER_STRONG)};
            }}
            QListWidget::item:selected {{
                background: {_qss_color(ACCENT)};
                color: {_qss_color(BG)};
            }}
        """)

    def set_workspace_root(self, path: Path) -> None:
        self._workspace_root = path
        self._populate()

    def _populate(self) -> None:
        self.clear()
        drones = DroneStore.list_drones(self._workspace_root)
        # Group by write policy
        read_only_items = []
        write_items = []
        for d in drones:
            policy = getattr(d, "write_policy", "read_only")
            if policy == "read_only":
                read_only_items.append(d)
            else:
                write_items.append(d)

        self._add_section_header("Read-only Drones")
        for d in read_only_items:
            self._add_drone_item(d)

        self._add_section_header("Write-capable Drones")
        for d in write_items:
            self._add_drone_item(d)

    def _add_section_header(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(FG_MUTED)
        font = QFont()
        font.setBold(True)
        font.setPointSize(9)
        item.setFont(font)
        self.addItem(item)

    def _add_drone_item(self, drone: DroneDefinition) -> None:
        item = QListWidgetItem(drone.name)
        item.setData(Qt.ItemDataRole.UserRole, drone.id)
        item.setToolTip(drone.description)
        self.addItem(item)

    def mimeData(self, items) -> QDrag:
        """Override to encode drone_id in custom mime type."""
        md = super().mimeData(items)
        current = self.currentItem()
        if current:
            drone_id = current.data(Qt.ItemDataRole.UserRole)
            if drone_id:
                md.setData("application/x-aura-drone-id", drone_id.encode("utf-8"))
        return md

    def startDrag(self, supportedActions) -> None:
        current = self.currentItem()
        if current and current.data(Qt.ItemDataRole.UserRole) is not None:
            drag = QDrag(self)
            md = self.mimeData([current])
            drag.setMimeData(md)
            pixmap = QPixmap(120, 30)
            pixmap.fill(QColor(BG_ALT))
            painter = QPainter(pixmap)
            painter.setPen(FG)
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, current.text())
            painter.end()
            drag.setPixmap(pixmap)
            drag.exec(Qt.DropAction.CopyAction)
        else:
            super().startDrag(supportedActions)

    def populate(self) -> None:
        """Public alias to refresh the palette."""
        self._populate()


class _PropertyPanel(QScrollArea):
    """Right sidebar showing chain/node/edge properties."""

    def __init__(self, editor: ChainEditor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self.setWidgetResizable(True)
        self.setStyleSheet(f"""
            QScrollArea {{
                background: {_qss_color(BG)};
                border: none;
            }}
        """)

        self._container = QWidget()
        self._container.setStyleSheet(f"background: {_qss_color(BG)};")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(6)
        self.setWidget(self._container)

        self._rebuild_chain_form()

    def clear(self) -> None:
        """Remove all child widgets from the layout."""
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                # Null editor refs to this widget before deleteLater
                if w is self._editor._chain_name_input:
                    self._editor._chain_name_input = None
                if w is self._editor._chain_desc_input:
                    self._editor._chain_desc_input = None
                if w is self._editor._chain_enabled_check:
                    self._editor._chain_enabled_check = None
                if w is self._editor._chain_schedule_input:
                    self._editor._chain_schedule_input = None
                w.deleteLater()

    def _add_label(self, text: str, bold: bool = False, color: QColor | None = None) -> QLabel:
        lbl = QLabel(text)
        if bold:
            f = QFont()
            f.setBold(True)
            lbl.setFont(f)
        if color:
            lbl.setStyleSheet(f"color: {_qss_color(color)}; background: transparent;")
        else:
            lbl.setStyleSheet(f"color: {_qss_color(FG)}; background: transparent;")
        self._layout.addWidget(lbl)
        return lbl

    def _add_separator(self) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_qss_color(BORDER)}; background: {_qss_color(BORDER)}; max-height: 1px;")
        self._layout.addWidget(sep)

    # ---- Chain form ----

    def _rebuild_chain_form(self) -> None:
        self.clear()
        self._editor._chain_name_input = QLineEdit()
        self._editor._chain_desc_input = QTextEdit()
        self._editor._chain_enabled_check = QCheckBox("Workflow enabled")
        self._editor._chain_schedule_input = QLineEdit()

        self._add_label("Workflow Properties", bold=True)
        self._add_separator()

        self._add_label("Name")
        self._editor._chain_name_input.setStyleSheet(f"""
            QLineEdit {{ background: {_qss_color(BG_RAISED)}; color: {_qss_color(FG)};
                          border: 1px solid {_qss_color(BORDER)}; border-radius: 3px; padding: 4px; }}
        """)
        self._layout.addWidget(self._editor._chain_name_input)

        self._add_label("Description")
        self._editor._chain_desc_input.setMaximumHeight(60)
        self._editor._chain_desc_input.setStyleSheet(f"""
            QTextEdit {{ background: {_qss_color(BG_RAISED)}; color: {_qss_color(FG)};
                         border: 1px solid {_qss_color(BORDER)}; border-radius: 3px; padding: 4px; }}
        """)
        self._layout.addWidget(self._editor._chain_desc_input)

        self._editor._chain_enabled_check.setStyleSheet(f"""
            QCheckBox {{ color: {_qss_color(FG)}; background: transparent; }}
        """)
        self._layout.addWidget(self._editor._chain_enabled_check)

        self._add_label("Schedule")
        self._editor._chain_schedule_input.setPlaceholderText("Manual only (future)")
        self._editor._chain_schedule_input.setEnabled(False)
        self._editor._chain_schedule_input.setStyleSheet(f"""
            QLineEdit {{ background: {_qss_color(BG_ALT)}; color: {_qss_color(FG_MUTED)};
                         border: 1px solid {_qss_color(BORDER)}; border-radius: 3px; padding: 4px; }}
        """)
        self._layout.addWidget(self._editor._chain_schedule_input)

        self._layout.addStretch()

        # Connect change signals
        self._editor._chain_name_input.textChanged.connect(self._editor._on_chain_property_changed)
        self._editor._chain_desc_input.textChanged.connect(self._editor._on_chain_property_changed)
        self._editor._chain_enabled_check.toggled.connect(self._editor._on_chain_property_changed)

    # ---- Node form ----

    def _rebuild_node_form(self, node_items: list[ChainNodeItem]) -> None:
        if not node_items:
            return
        self.clear()
        node = node_items[0]  # Show first selected

        if node.is_draft:
            # Draft node form
            self._add_label("Draft Drone", bold=True, color=QColor("#9b8bb5"))
            self._add_separator()

            # Draft name (editable)
            self._add_label("Name")
            name_input = QLineEdit()
            name_input.setText(node.draft_name)
            name_input.setStyleSheet(f"""
                QLineEdit {{ background: {_qss_color(BG_RAISED)}; color: {_qss_color(FG)};
                             border: 1px solid {_qss_color(BORDER)}; border-radius: 3px; padding: 4px; }}
            """)
            name_input.textChanged.connect(
                lambda text, n=node: setattr(n, 'draft_name', text)
            )
            self._layout.addWidget(name_input)

            # Draft accepts / produces (read-only)
            self._add_label(f"In: {node.draft_accepts or '?'}", color=FG_DIM)
            self._add_label(f"Out: {node.draft_produces or '?'}", color=FG_DIM)

            # Draft brief (read-only)
            if node.draft_brief:
                self._add_label(f"Brief: {node.draft_brief}", color=FG_MUTED)

            self._add_separator()

            # Remove button
            remove_btn = QPushButton("Remove Draft")
            remove_btn.setStyleSheet(f"""
                QPushButton {{ background: {_qss_color(DANGER)}; color: white; border: none;
                              border-radius: 4px; padding: 6px; }}
                QPushButton:hover {{ background: {_qss_darker(DANGER, 120)}; }}
            """)
            remove_btn.clicked.connect(lambda: self._editor._canvas._remove_node(node))
            self._layout.addWidget(remove_btn)

            self._add_label(
                "Draft Drone — save as a real Drone before running this workflow.",
                color=FG_MUTED,
            )

            self._layout.addStretch()
            return

        self._add_label("Node Properties", bold=True)
        self._add_separator()

        # Drone name
        name = node.drone.name if node.drone else "(missing)"
        if node.missing:
            name += " (missing)"
        name_lbl = self._add_label(name, bold=True)
        if node.missing:
            name_lbl.setStyleSheet(f"color: {_qss_color(DANGER)}; background: transparent;")

        # Write policy
        policy = getattr(node.drone, "write_policy", "read_only") if node.drone else "?"
        policy_color = SUCCESS if policy == "read_only" else WARN
        self._add_label(f"Policy: {policy}", color=policy_color)

        # Accepts / Produces
        accepts = getattr(node.drone, "accepts", None) or "any"
        produces = getattr(node.drone, "produces", None) or "any"
        self._add_label(f"In: {accepts}", color=FG_DIM)
        self._add_label(f"Out: {produces}", color=FG_DIM)

        self._add_separator()

        # Goal template
        self._add_label("Goal Template")
        goal_input = QTextEdit()
        goal_input.setPlainText(node.goal_template)
        goal_input.setMaximumHeight(80)
        goal_input.setPlaceholderText("Optional per-node goal. Overrides drone default.")
        goal_input.setStyleSheet(f"""
            QTextEdit {{ background: {_qss_color(BG_RAISED)}; color: {_qss_color(FG)};
                         border: 1px solid {_qss_color(BORDER)}; border-radius: 3px; padding: 4px; }}
        """)
        goal_input.textChanged.connect(
            lambda: self._on_goal_changed(node, goal_input.toPlainText())
        )
        self._layout.addWidget(goal_input)

        self._add_separator()

        # Remove button
        remove_btn = QPushButton("Remove Node")
        remove_btn.setStyleSheet(f"""
            QPushButton {{ background: {_qss_color(DANGER)}; color: white; border: none;
                          border-radius: 4px; padding: 6px; }}
            QPushButton:hover {{ background: {_qss_darker(DANGER, 120)}; }}
        """)
        remove_btn.clicked.connect(lambda: self._editor._canvas._remove_node(node))
        self._layout.addWidget(remove_btn)

        self._layout.addStretch()

    def _on_goal_changed(self, node: ChainNodeItem, goal: str) -> None:
        if node.is_draft:
            return
        node.goal_template = goal
        node.update()
        self._editor._on_canvas_changed()

    # ---- Edge form ----

    def _rebuild_edge_form(self, edge_items: list[ChainEdgeItem]) -> None:
        if not edge_items:
            return
        self.clear()
        edge = edge_items[0]

        self._add_label("Edge Properties", bold=True)
        self._add_separator()

        from_node = self._editor._canvas._nodes.get(edge.from_node_id)
        to_node = self._editor._canvas._nodes.get(edge.to_node_id)
        from_name = from_node.drone.name if from_node and from_node.drone else edge.from_node_id
        to_name = to_node.drone.name if to_node and to_node.drone else edge.to_node_id

        self._add_label(f"From: {from_name}", color=FG)
        self._add_label(f"To: {to_name}", color=FG)

        # Compatibility check
        if from_node and from_node.drone and to_node and to_node.drone:
            from_type_name = getattr(from_node.drone, "produces", None)
            to_type_name = getattr(to_node.drone, "accepts", None)
            if from_type_name and to_type_name:
                from_at = BUILTIN_TYPES.get(from_type_name)
                to_at = BUILTIN_TYPES.get(to_type_name)
                if from_at is None or to_at is None:
                    self._add_label(
                        f"Type compatibility: Unknown type ({from_type_name} \u2192 {to_type_name})",
                        color=WARN,
                    )
                elif is_compatible(from_at, to_at):
                    self._add_label("Type compatibility: Compatible", color=SUCCESS)
                else:
                    self._add_label(
                        f"Type compatibility: Incompatible ({from_type_name} \u2192 {to_type_name})",
                        color=DANGER,
                    )
            elif not from_type_name and not to_type_name:
                self._add_label("Type compatibility: No contracts", color=FG_MUTED)
            elif not from_type_name:
                self._add_label(
                    f"Type compatibility: No output from {from_name}",
                    color=WARN,
                )
            else:
                self._add_label("Type compatibility: Compatible", color=SUCCESS)
        else:
            self._add_label("Type compatibility: Unknown", color=FG_MUTED)

        self._add_separator()

        remove_btn = QPushButton("Remove Edge")
        remove_btn.setStyleSheet(f"""
            QPushButton {{ background: {_qss_color(DANGER)}; color: white; border: none;
                          border-radius: 4px; padding: 6px; }}
            QPushButton:hover {{ background: {_qss_darker(DANGER, 120)}; }}
        """)
        remove_btn.clicked.connect(lambda: self._editor._canvas._remove_edge(edge))
        self._layout.addWidget(remove_btn)

        self._layout.addStretch()

    # ---- Public rebuild ----

    def rebuild(self) -> None:
        selection = self._editor._canvas.scene().selectedItems()
        node_items = [i for i in selection if isinstance(i, ChainNodeItem)]
        edge_items = [i for i in selection if isinstance(i, ChainEdgeItem)]

        if node_items:
            self._rebuild_node_form(node_items)
        elif edge_items:
            self._rebuild_edge_form(edge_items)
        else:
            self._rebuild_chain_form()


class ChainEditor(QWidget):
    """Full-view chain editor with toolbar, palette, canvas, and property panel."""

    runChainRequested = Signal(str)
    goBackRequested = Signal()
    settle_draft_requested = Signal(object)  # dict: {"brief": DroneBuildBrief, "draft_node_id": str}

    def __init__(
        self,
        workspace_root: Path,
        chain_id: str | None = None,
        parent=None,
        provider_id: str = "deepseek",
        model: str = "",
        thinking: str = "disabled",
        temperature: float = 0.4,
    ):
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._provider_id = provider_id
        self._model = model
        self._thinking = thinking
        self._temperature = temperature

        # Chain identity (set after load or creation)
        self._chain_id: str = ""
        self._chain_name: str = "New Workflow"
        self._chain_description: str = ""
        self._chain_enabled: bool = True
        self._chain_schedule: str = ""

        # Widget references (set up by property panel)
        self._chain_name_input: QLineEdit | None = None
        self._chain_desc_input: QTextEdit | None = None
        self._chain_enabled_check: QCheckBox | None = None
        self._chain_schedule_input: QLineEdit | None = None

        # Runtime state — must be initialized before layout/load which may reference them
        self._workshop_draft_node_id: str | None = None
        self._palette_width = 260
        self._dirty = False

        # Auto-save debounce timer — must exist before _on_canvas_changed() fires
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(300)
        self._auto_save_timer.timeout.connect(self._save_chain)

        # Build layout
        self._build_layout()

        # Wire selection changes to panel switching
        self._canvas._scene.selectionChanged.connect(self._on_selection_changed)

        # Load or create chain
        self._load_or_create_chain(chain_id)

    # ---- Layout ----

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = self._build_toolbar()
        layout.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter = splitter
        splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background: {_qss_color(BORDER)};
                width: 1px;
            }}
        """)

        self._palette = _DronePaletteList(self._workspace_root, self)

        self._mode_buttons: dict[str, QToolButton] = {}
        self._left_stack = QStackedWidget()

        # ---- Mode switcher (pill-tab bar) ----
        mode_bar = QWidget()
        mode_layout = QHBoxLayout(mode_bar)
        mode_layout.setContentsMargins(4, 4, 4, 2)
        mode_layout.setSpacing(3)
        for name in ("Workflows", "Drones", "Details", "Workshop"):
            btn = QToolButton()
            btn.setText(name)
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.clicked.connect(lambda checked, n=name: self._set_active_mode(n))
            mode_layout.addWidget(btn)
            self._mode_buttons[name] = btn
        mode_layout.addStretch()

        # ---- Page 0: Workflows ----
        workflows_page = QWidget()
        wf_layout = QVBoxLayout(workflows_page)
        wf_layout.setContentsMargins(0, 4, 0, 0)
        wf_layout.setSpacing(4)
        wf_header = QLabel("Workflows")
        wf_header.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {_qss_color(FG_MUTED)}; padding: 0 8px;")
        new_wf_btn = QPushButton("+ New")
        new_wf_btn.setFlat(True)
        new_wf_btn.setCursor(Qt.PointingHandCursor)
        new_wf_btn.setStyleSheet(
            f"QPushButton {{ color: {_qss_color(FG_MUTED)}; font-size: 11px; padding: 2px 6px; border: none; }}"
            f"QPushButton:hover {{ color: {_qss_color(ACCENT)}; }}"
        )
        new_wf_btn.clicked.connect(self._on_new_workflow_from_list)
        wf_header_row = QHBoxLayout()
        wf_header_row.setContentsMargins(0, 0, 0, 0)
        wf_header_row.addWidget(wf_header)
        wf_header_row.addStretch()
        wf_header_row.addWidget(new_wf_btn)
        wf_layout.addLayout(wf_header_row)
        self._workflow_list_pane = WorkflowListPane(self._workspace_root, self)
        self._workflow_list_pane.editWorkflowRequested.connect(self._on_load_existing_workflow)
        self._workflow_list_pane.newWorkflowRequested.connect(self._on_new_workflow_from_list)
        self._workflow_list_pane.deleteWorkflowRequested.connect(self._on_delete_workflow_from_list)
        self._workflow_list_pane.runWorkflowRequested.connect(self._on_run_workflow_from_list)
        wf_layout.addWidget(self._workflow_list_pane, 1)
        self._left_stack.addWidget(workflows_page)  # index 0

        # ---- Page 1: Drones ----
        palette_container = QWidget()
        palette_layout = QVBoxLayout(palette_container)
        palette_layout.setContentsMargins(0, 4, 0, 0)
        palette_layout.setSpacing(4)
        palette_header_row = QHBoxLayout()
        palette_header_row.setContentsMargins(0, 0, 0, 0)
        palette_header_label = QLabel("Drone Palette")
        palette_header_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {_qss_color(FG_MUTED)}; padding: 0 8px;")
        palette_header_row.addWidget(palette_header_label)
        palette_header_row.addStretch()
        new_drone_btn = QPushButton("+ New Drone")
        new_drone_btn.setFlat(True)
        new_drone_btn.setCursor(Qt.PointingHandCursor)
        new_drone_btn.setStyleSheet(
            f"QPushButton {{ color: {_qss_color(FG_MUTED)}; font-size: 11px; padding: 2px 6px; border: none; }}"
            f"QPushButton:hover {{ color: {_qss_color(ACCENT)}; }}"
        )
        new_drone_btn.clicked.connect(self._on_new_drone_clicked)
        palette_header_row.addWidget(new_drone_btn)
        palette_layout.addLayout(palette_header_row)
        palette_layout.addWidget(self._palette, 1)
        self._left_stack.addWidget(palette_container)  # index 1

        # ---- Page 2: Details ----
        self._property_panel = _PropertyPanel(self)
        details_page = QWidget()
        details_layout = QVBoxLayout(details_page)
        details_layout.setContentsMargins(0, 4, 0, 0)
        details_layout.setSpacing(4)
        details_header = QLabel("Properties")
        details_header.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {_qss_color(FG_MUTED)}; padding: 0 8px;")
        details_layout.addWidget(details_header)
        details_layout.addWidget(self._property_panel, 1)
        self._left_stack.addWidget(details_page)  # index 2

        self._workshop_panel = None
        self._workshop_container = None

        # ---- Left panel: mode bar + stack ----
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(mode_bar)
        left_layout.addWidget(self._left_stack, 1)
        splitter.addWidget(left_panel)

        # ---- Center: canvas ----
        self._canvas = ChainCanvas(self)
        self._canvas.canvasChanged.connect(self._on_canvas_changed)
        splitter.addWidget(self._canvas)

        splitter.setSizes([220, 800])
        layout.addWidget(splitter, 1)

        # Default to Drones mode after all pages exist
        self._set_active_mode("Drones")
        self._workflow_list_pane.refresh()

        # Status bar
        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet(f"""
            background: {_qss_color(BG_ALT)}; color: {_qss_color(FG_MUTED)};
            padding: 4px 8px; border-top: 1px solid {_qss_color(BORDER)};
        """)
        layout.addWidget(self._status_label)

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setStyleSheet(f"background: {_qss_color(BG_ALT)}; border-bottom: 1px solid {_qss_color(BORDER)};")
        t_layout = QHBoxLayout(toolbar)
        t_layout.setContentsMargins(8, 4, 8, 4)
        t_layout.setSpacing(4)

        self._title_label = QLabel(self._chain_name)
        self._title_label.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {_qss_color(FG)}; padding-right: 12px;")
        t_layout.addWidget(self._title_label)

        btn_style = f"""
            QPushButton {{
                background: {_qss_color(BG_RAISED)}; color: {_qss_color(FG)};
                border: 1px solid {_qss_color(BORDER)}; border-radius: 4px;
                padding: 6px 14px; font-size: 12px;
            }}
            QPushButton:hover {{
                background: {_qss_color(BORDER)}; border-color: {_qss_color(BORDER_STRONG)};
            }}
            QPushButton:pressed {{
                background: {_qss_color(BORDER_STRONG)};
            }}
        """

        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(btn_style)
        save_btn.clicked.connect(self._save_chain)
        t_layout.addWidget(save_btn)

        save_as_btn = QPushButton("Save As")
        save_as_btn.setStyleSheet(btn_style)
        save_as_btn.clicked.connect(self._save_chain_as)
        t_layout.addWidget(save_as_btn)

        validate_btn = QPushButton("Validate")
        validate_btn.setStyleSheet(btn_style)
        validate_btn.clicked.connect(self._validate_chain)
        t_layout.addWidget(validate_btn)

        run_btn = QPushButton("Run")
        run_btn.setStyleSheet(btn_style)
        run_btn.clicked.connect(self._on_run_clicked)
        t_layout.addWidget(run_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet(btn_style)
        delete_btn.clicked.connect(self._on_delete_clicked)
        t_layout.addWidget(delete_btn)

        t_layout.addStretch()

        back_btn = QPushButton("\u2190 Close")
        back_btn.setStyleSheet(btn_style)
        back_btn.clicked.connect(self.goBackRequested.emit)
        t_layout.addWidget(back_btn)

        return toolbar

    # ---- Load / create chain ----

    def _load_or_create_chain(self, chain_id: str | None) -> None:
        if chain_id:
            chain = ChainStore.load_chain(self._workspace_root, chain_id)
            if chain is not None:
                self._chain_id = chain.id
                self._chain_name = chain.name or "Untitled"
                self._chain_description = chain.description or ""
                self._chain_enabled = chain.enabled
                self._chain_schedule = chain.schedule or ""

                # Load drones for lookup
                drone_lookup = self._build_drone_lookup()

                # Load canvas
                self._canvas.load_chain(chain, drone_lookup)

                # Update form
                self._sync_form_from_chain()

                self._dirty = False
                self._update_title_label()
                self.set_status("Loaded.", SUCCESS)
                return

        from datetime import datetime, timezone
        self._chain_id = ChainStore.next_id(self._workspace_root, self._chain_name)
        self._chain_name = "New Workflow"
        self._chain_description = ""
        self._chain_enabled = True
        self._chain_schedule = ""

        drone_lookup = self._build_drone_lookup()
        chain = ChainDefinition(
            id=self._chain_id,
            name=self._chain_name,
            description=self._chain_description,
            enabled=self._chain_enabled,
            nodes=[],
            edges=[],
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._canvas.load_chain(chain, drone_lookup)
        self._sync_form_from_chain()
        self._dirty = False
        self._update_title_label()
        self.set_status("New workflow.", FG_MUTED)

    def _build_drone_lookup(self) -> dict[str, DroneDefinition]:
        drones = DroneStore.list_drones(self._workspace_root)
        return {d.id: d for d in drones}

    def _sync_form_from_chain(self) -> None:
        if _widget_valid(self._chain_name_input):
            self._chain_name_input.blockSignals(True)
            self._chain_name_input.setText(self._chain_name)
            self._chain_name_input.blockSignals(False)
        if _widget_valid(self._chain_desc_input):
            self._chain_desc_input.blockSignals(True)
            self._chain_desc_input.setPlainText(self._chain_description)
            self._chain_desc_input.blockSignals(False)
        if _widget_valid(self._chain_enabled_check):
            self._chain_enabled_check.blockSignals(True)
            self._chain_enabled_check.setChecked(self._chain_enabled)
            self._chain_enabled_check.blockSignals(False)
        if _widget_valid(self._chain_schedule_input):
            self._chain_schedule_input.blockSignals(True)
            self._chain_schedule_input.setText(self._chain_schedule)
            self._chain_schedule_input.blockSignals(False)

    def _sync_chain_from_form(self) -> None:
        if _widget_valid(self._chain_name_input):
            self._chain_name = self._chain_name_input.text()
        if _widget_valid(self._chain_desc_input):
            self._chain_description = self._chain_desc_input.toPlainText()
        if _widget_valid(self._chain_enabled_check):
            self._chain_enabled = self._chain_enabled_check.isChecked()

    # ---- Snapshots & Save ----

    def _snapshot_chain(self) -> ChainDefinition:
        """Build a ChainDefinition from current canvas and form state."""
        from datetime import datetime, timezone
        self._sync_chain_from_form()

        nodes_data, edges_data = self._canvas.to_chain_nodes_and_edges()

        chain_nodes = [
            ChainNode(id=n["id"], drone_id=n["drone_id"],
                      goal_template=n["goal_template"], position=n["position"],
                      is_draft=n.get("is_draft", False),
                      draft_name=n.get("draft_name", ""),
                      draft_accepts=n.get("draft_accepts", ""),
                      draft_produces=n.get("draft_produces", ""),
                      draft_brief=n.get("draft_brief", ""))
            for n in nodes_data
        ]
        chain_edges = [
            ChainEdge(from_node=e["from_node"], to_node=e["to_node"])
            for e in edges_data
        ]

        now = datetime.now(timezone.utc).isoformat()
        return ChainDefinition(
            id=self._chain_id,
            name=self._chain_name,
            description=self._chain_description,
            enabled=self._chain_enabled,
            schedule=self._chain_schedule,
            nodes=chain_nodes,
            edges=chain_edges,
            created_at=now,
            updated_at=now,
        )

    def _save_chain(self) -> None:
        try:
            chain = self._snapshot_chain()
            ChainStore.save_chain(self._workspace_root, chain)
            self._dirty = False
            self._update_title_label()
            self._workflow_list_pane.refresh()
            self.set_status("Saved.", SUCCESS)
        except Exception as exc:
            logger.exception("Failed to save chain")
            self.set_status(f"Save failed: {exc}", DANGER)

    # ---- Validate ----

    def _validate_chain(self) -> None:
        try:
            chain = self._snapshot_chain()
            drone_lookup = self._build_drone_lookup()
            result = validate(chain, drone_lookup)
            if not result.ok:
                msg = "; ".join(result.errors[:5])
                if len(result.errors) > 5:
                    msg += f" (+{len(result.errors) - 5} more)"
                self.set_status(f"Validation: {msg}", DANGER)
            else:
                self.set_status("Validation: Valid", SUCCESS)
        except Exception as exc:
            self.set_status(f"Validation error: {exc}", DANGER)

    # ---- Callbacks ----

    def _on_canvas_changed(self) -> None:
        self._dirty = True
        self._auto_save_timer.start()
        # Update property panel on selection change
        self._property_panel.rebuild()
        self._update_left_panel_mode()
        self._update_title_label()

    def _on_chain_property_changed(self) -> None:
        # Sync form state into chain attributes immediately so edits survive panel rebuild
        self._sync_chain_from_form()
        self._on_canvas_changed()

    def _on_selection_changed(self) -> None:
        self._update_left_panel_mode()

    # ---- Left panel switching (palette vs. workshop) ----

    def _ensure_workshop_panel(self) -> QWidget:
        if self._workshop_container is not None:
            return self._workshop_container

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Back button
        back_btn = QPushButton("\u2190 Back to Drones")
        back_btn.setFlat(True)
        back_btn.setCursor(Qt.PointingHandCursor)
        back_btn.setStyleSheet(f"QPushButton {{ color: {_qss_color(FG_MUTED)}; text-align: left; padding: 2px; }}")
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
        self._left_stack.addWidget(container)  # index 3
        return container

    def _update_left_panel_mode(self) -> None:
        selection = self._canvas._scene.selectedItems()
        draft_node = None
        non_draft = None
        for item in selection:
            if isinstance(item, ChainNodeItem):
                if item.is_draft:
                    draft_node = item
                    break
                else:
                    non_draft = item
            elif isinstance(item, ChainEdgeItem) and non_draft is None:
                non_draft = item

        if draft_node:
            if self._left_stack.currentIndex() != 3:
                self._palette_width = self._splitter.sizes()[0]
            self._show_workshop_for_draft(draft_node)
            self._splitter.setSizes([340, self._splitter.sizes()[1]])
            self._set_active_mode("Workshop")
        elif non_draft:
            self._set_active_mode("Details")
            self._property_panel.rebuild()
        # Nothing selected → do not auto-switch

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
        self._ws_contracts_label.setText(f"In: {accepts}  \u00b7  Out: {produces}")

        if self._left_stack.currentIndex() != 3:
            self._left_stack.setCurrentIndex(3)

    def _on_back_to_palette(self) -> None:
        self._workshop_draft_node_id = None
        if self._workshop_panel is not None:
            self._workshop_panel.reset_workshop_state()
        self._canvas._scene.clearSelection()
        self._set_active_mode("Drones")

    # ---- Mode switcher ----

    def _set_active_mode(self, name: str) -> None:
        index_map = {"Workflows": 0, "Drones": 1, "Details": 2, "Workshop": 3}
        target = index_map[name]

        if name == "Workshop":
            if self._left_stack.currentIndex() != target:
                self._palette_width = self._splitter.sizes()[0]
            self._splitter.setSizes([340, self._splitter.sizes()[1]])
        else:
            if self._left_stack.currentIndex() == index_map.get("Workshop", 3):
                self._splitter.setSizes([self._palette_width, self._splitter.sizes()[1]])

        self._left_stack.setCurrentIndex(target)

        for mode_name, btn in self._mode_buttons.items():
            if mode_name == name:
                btn.setChecked(True)
                btn.setStyleSheet(f"""
                    QToolButton {{
                        background: {_qss_color(ACCENT)}; color: {_qss_color(BG)};
                        border: 1px solid {_qss_color(ACCENT)}; border-radius: 4px;
                        padding: 6px 10px; font-weight: 600;
                    }}
                """)
            else:
                btn.setChecked(False)
                btn.setStyleSheet(f"""
                    QToolButton {{
                        background: transparent; color: {_qss_color(FG_MUTED)};
                        border: 1px solid {_qss_color(BORDER)}; border-radius: 4px;
                        padding: 6px 10px;
                    }}
                    QToolButton:hover {{
                        background: {_qss_color(BG_RAISED)}; color: {_qss_color(FG)};
                    }}
                """)

    # ---- Workflow list callbacks ----

    def _on_load_existing_workflow(self, chain_id: str) -> None:
        choice = self._prompt_save_changes()
        if choice == "cancel":
            return
        if choice == "save":
            self._save_chain()
        self._load_or_create_chain(chain_id)
        self._workflow_list_pane.refresh()

    def _on_new_workflow_from_list(self) -> None:
        choice = self._prompt_save_changes()
        if choice == "cancel":
            return
        if choice == "save":
            self._save_chain()
        self._load_or_create_chain(None)
        self._workflow_list_pane.refresh()

    def _on_delete_workflow_from_list(self, chain_id: str) -> None:
        reply = QMessageBox.question(
            self,
            "Delete Workflow",
            "Delete this workflow? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            ChainStore.delete_chain(self._workspace_root, chain_id)
        except Exception as exc:
            self.set_status(f"Delete failed: {exc}", DANGER)
            return
        if chain_id == self._chain_id:
            self._load_or_create_chain(None)
        self._workflow_list_pane.refresh()

    def _on_run_workflow_from_list(self, chain_id: str) -> None:
        self._save_chain()
        self.runChainRequested.emit(chain_id)

    def _on_new_drone_clicked(self) -> None:
        if self._workspace_root is None:
            return
        view_center = self._canvas.viewport().rect().center()
        scene_pos = self._canvas.mapToScene(view_center)
        node_item = self._canvas._canvas_add_draft_node(scene_pos)
        self._canvas._scene.clearSelection()
        node_item.setSelected(True)
        canvasChanged = getattr(self._canvas, 'canvasChanged', None)
        if canvasChanged:
            canvasChanged.emit()

    def _on_workshop_build_requested(self, brief: object) -> None:
        logger.info("Workshop build requested, brief: %s", type(brief).__name__)

        # Find the selected draft node on the canvas
        draft_node_id = None
        for item in self._canvas._scene.selectedItems():
            dio = getattr(item, 'data', None)
            if dio and getattr(dio, 'kind', None) == 'draft':
                nd = getattr(item, 'node', None)
                data = getattr(nd, 'data', None) if nd else dio
                draft_node_id = data.get('id') if isinstance(data, dict) else (getattr(data, 'id', None) or getattr(nd, 'id', None))
                break
        if not draft_node_id:
            fallback = self._workshop_draft_node_id
            if fallback:
                draft_node_id = fallback
            else:
                self.set_status("No draft node selected", DANGER)
                return

        self.settle_draft_requested.emit({"brief": brief, "draft_node_id": draft_node_id})
        self.set_status("Building drone\u2026")

    def settle_draft_node(self, node_id: str, drone_def) -> bool:
        """Replace a draft node with the real saved DroneDefinition."""
        item = self._canvas._nodes.get(node_id)
        if item is None:
            logger.warning(f"Cannot settle draft: node {node_id} not found on canvas")
            return False
        if not item.is_draft:
            logger.warning(f"Cannot settle draft: node {node_id} is no longer a draft")
            return False

        # Mutate the ChainNodeItem to become a real drone node
        item._is_draft = False
        item._drone = drone_def
        item._drone_id = drone_def.id
        item._draft_name = ""
        item._draft_accepts = ""
        item._draft_produces = ""
        item._draft_brief = ""
        item._missing = False
        item.update()  # trigger repaint

        # Persist chain
        self._save_chain()

        # Refresh palette to show the new drone
        self._palette.populate()

        # Reset workshop state
        self._workshop_draft_node_id = None

        # Switch left panel back to palette, refresh property panel
        self._canvas._scene.clearSelection()
        item.setSelected(True)
        self._update_left_panel_mode()
        self._property_panel.rebuild()

        return True

    def _on_run_clicked(self) -> None:
        self._save_chain()
        self.runChainRequested.emit(self._chain_id)

    def _on_delete_clicked(self) -> None:
        reply = QMessageBox.question(
            self,
            "Delete Workflow",
            f"Are you sure you want to delete '{self._chain_name}'?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                ChainStore.delete_chain(self._workspace_root, self._chain_id)
                self.set_status("Deleted.", SUCCESS)
                self.goBackRequested.emit()
            except Exception as exc:
                self.set_status(f"Delete failed: {exc}", DANGER)

    # ---- Title ----

    def _update_title_label(self) -> None:
        if not hasattr(self, '_title_label'):
            return
        if self._dirty:
            self._title_label.setText(f"\u2022 {self._chain_name}")
            self._title_label.setStyleSheet(
                f"font-size: 15px; font-weight: bold; color: {_qss_color(ACCENT)}; padding-right: 12px;"
            )
        else:
            self._title_label.setText(self._chain_name)
            self._title_label.setStyleSheet(
                f"font-size: 15px; font-weight: bold; color: {_qss_color(FG)}; padding-right: 12px;"
            )

    # ---- Save As ----

    def _save_chain_as(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(
            self, "Save Workflow As", "New workflow name:",
            text=self._chain_name,
        )
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()
        old_id = self._chain_id
        try:
            self._chain_name = new_name
            self._chain_id = ChainStore.next_id(self._workspace_root, new_name)
            chain = self._snapshot_chain()
            from dataclasses import replace
            chain = replace(chain, id=self._chain_id, name=self._chain_name)
            ChainStore.save_chain(self._workspace_root, chain)
            self._dirty = False
            self._update_title_label()
            self._workflow_list_pane.refresh()
            self.set_status(f"Saved as '{new_name}'.", SUCCESS)
        except Exception as exc:
            self._chain_id = old_id
            self.set_status(f"Save As failed: {exc}", DANGER)

    # ---- Unsaved changes prompt ----

    def _prompt_save_changes(self) -> str:
        """Returns 'save', 'discard', or 'cancel'."""
        if not self._dirty:
            return "discard"
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Changes")
        box.setText(f"'{self._chain_name}' has unsaved changes.")
        box.setInformativeText("Do you want to save before switching?")
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == save_btn:
            return "save"
        elif clicked == discard_btn:
            return "discard"
        return "cancel"

    # ---- Status bar ----

    def set_status(self, message: str, color: QColor | str = FG_MUTED) -> None:
        color_value = color.name() if hasattr(color, "name") else str(color)
        self._status_label.setText(message)
        self._status_label.setStyleSheet(f"""
            background: {_qss_color(BG_ALT)}; color: {color_value};
            padding: 4px 8px; border-top: 1px solid {_qss_color(BORDER)};
        """)

    # ---- Workspace root ----

    def set_workspace_root(self, path: Path) -> None:
        self._workspace_root = path
        self._palette.set_workspace_root(path)
        self._workflow_list_pane.set_workspace_root(path)
        self._workflow_list_pane.refresh()
        self._load_or_create_chain(self._chain_id)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root
