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
                background: {BG_RAISED.name()};
                border: none;
                padding: 4px;
                color: {FG.name()};
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-radius: 4px;
                border: 1px solid {BORDER.name()};
                margin: 2px 0;
                background: {BG_ALT.name()};
            }}
            QListWidget::item:hover {{
                background: {BG.name()};
                border: 1px solid {BORDER_STRONG.name()};
            }}
            QListWidget::item:selected {{
                background: {ACCENT.name()};
                color: {BG.name()};
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
        self.setMinimumWidth(220)
        self.setMaximumWidth(320)
        self.setWidgetResizable(True)
        self.setStyleSheet(f"""
            QScrollArea {{
                background: {BG.name()};
                border: none;
                border-left: 1px solid {BORDER.name()};
            }}
        """)

        self._container = QWidget()
        self._container.setStyleSheet(f"background: {BG.name()};")
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
                w.deleteLater()

    def _add_label(self, text: str, bold: bool = False, color: QColor | None = None) -> QLabel:
        lbl = QLabel(text)
        if bold:
            f = QFont()
            f.setBold(True)
            lbl.setFont(f)
        if color:
            lbl.setStyleSheet(f"color: {color.name()}; background: transparent;")
        else:
            lbl.setStyleSheet(f"color: {FG.name()}; background: transparent;")
        self._layout.addWidget(lbl)
        return lbl

    def _add_separator(self) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {BORDER.name()}; background: {BORDER.name()}; max-height: 1px;")
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
            QLineEdit {{ background: {BG_RAISED.name()}; color: {FG.name()};
                          border: 1px solid {BORDER.name()}; border-radius: 3px; padding: 4px; }}
        """)
        self._layout.addWidget(self._editor._chain_name_input)

        self._add_label("Description")
        self._editor._chain_desc_input.setMaximumHeight(60)
        self._editor._chain_desc_input.setStyleSheet(f"""
            QTextEdit {{ background: {BG_RAISED.name()}; color: {FG.name()};
                         border: 1px solid {BORDER.name()}; border-radius: 3px; padding: 4px; }}
        """)
        self._layout.addWidget(self._editor._chain_desc_input)

        self._editor._chain_enabled_check.setStyleSheet(f"""
            QCheckBox {{ color: {FG.name()}; background: transparent; }}
        """)
        self._layout.addWidget(self._editor._chain_enabled_check)

        self._add_label("Schedule")
        self._editor._chain_schedule_input.setPlaceholderText("Manual only (future)")
        self._editor._chain_schedule_input.setEnabled(False)
        self._editor._chain_schedule_input.setStyleSheet(f"""
            QLineEdit {{ background: {BG_ALT.name()}; color: {FG_MUTED.name()};
                         border: 1px solid {BORDER.name()}; border-radius: 3px; padding: 4px; }}
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
                QLineEdit {{ background: {BG_RAISED.name()}; color: {FG.name()};
                             border: 1px solid {BORDER.name()}; border-radius: 3px; padding: 4px; }}
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
                QPushButton {{ background: {DANGER.name()}; color: white; border: none;
                              border-radius: 4px; padding: 6px; }}
                QPushButton:hover {{ background: {DANGER.darker(120).name()}; }}
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
            name_lbl.setStyleSheet(f"color: {DANGER.name()}; background: transparent;")

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
            QTextEdit {{ background: {BG_RAISED.name()}; color: {FG.name()};
                         border: 1px solid {BORDER.name()}; border-radius: 3px; padding: 4px; }}
        """)
        goal_input.textChanged.connect(
            lambda: self._on_goal_changed(node, goal_input.toPlainText())
        )
        self._layout.addWidget(goal_input)

        self._add_separator()

        # Remove button
        remove_btn = QPushButton("Remove Node")
        remove_btn.setStyleSheet(f"""
            QPushButton {{ background: {DANGER.name()}; color: white; border: none;
                          border-radius: 4px; padding: 6px; }}
            QPushButton:hover {{ background: {DANGER.darker(120).name()}; }}
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
                        f"Type compatibility: Unknown type ({from_type_name} → {to_type_name})",
                        color=WARN,
                    )
                elif is_compatible(from_at, to_at):
                    self._add_label("Type compatibility: Compatible", color=SUCCESS)
                else:
                    self._add_label(
                        f"Type compatibility: Incompatible ({from_type_name} → {to_type_name})",
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
            QPushButton {{ background: {DANGER.name()}; color: white; border: none;
                          border-radius: 4px; padding: 6px; }}
            QPushButton:hover {{ background: {DANGER.darker(120).name()}; }}
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

        # Build layout
        self._build_layout()

        # Wire selection changes to panel switching
        self._canvas._scene.selectionChanged.connect(self._on_selection_changed)

        # Load or create chain
        self._load_or_create_chain(chain_id)

        # Workshop draft settlement
        self._workshop_draft_node_id: str | None = None
        self._palette_width = 260

        # Auto-save debounce timer
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(300)
        self._auto_save_timer.timeout.connect(self._save_chain)

    # ---- Layout ----

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        toolbar = self._build_toolbar()
        layout.addWidget(toolbar)

        # Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter = splitter
        splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background: {BORDER.name()};
                width: 1px;
            }}
        """)

        # Left: drone palette (behind QStackedWidget)
        self._palette = _DronePaletteList(self._workspace_root, self)

        self._left_stack = QStackedWidget()

        palette_container = QWidget()
        palette_layout = QVBoxLayout(palette_container)
        palette_layout.setContentsMargins(0, 4, 0, 0)
        palette_layout.setSpacing(4)

        palette_header = QLabel("Drone Palette")
        palette_header.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {FG_MUTED.name()}; padding: 0 8px;")
        palette_layout.addWidget(palette_header)
        palette_layout.addWidget(self._palette, 1)

        self._left_stack.addWidget(palette_container)  # index 0: palette

        self._workshop_panel = None          # created lazily
        self._workshop_container = None      # created lazily

        splitter.addWidget(self._left_stack)

        # Center: canvas
        self._canvas = ChainCanvas(self)
        self._canvas.canvasChanged.connect(self._on_canvas_changed)
        splitter.addWidget(self._canvas)

        # Right: property panel
        self._property_panel = _PropertyPanel(self)
        splitter.addWidget(self._property_panel)

        # Set initial sizes: palette ~150px, canvas stretch, property ~280px
        splitter.setSizes([150, 600, 280])
        layout.addWidget(splitter, 1)

        # Status bar
        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet(f"""
            background: {BG_ALT.name()}; color: {FG_MUTED.name()};
            padding: 4px 8px; border-top: 1px solid {BORDER.name()};
        """)
        layout.addWidget(self._status_label)

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setStyleSheet(f"background: {BG_ALT.name()}; border-bottom: 1px solid {BORDER.name()};")
        t_layout = QHBoxLayout(toolbar)
        t_layout.setContentsMargins(8, 4, 8, 4)
        t_layout.setSpacing(4)

        title_label = QLabel("Workflow Studio")
        title_label.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {FG.name()}; padding-right: 12px;")
        t_layout.addWidget(title_label)

        btn_style = f"""
            QPushButton {{
                background: {BG_RAISED.name()}; color: {FG.name()};
                border: 1px solid {BORDER.name()}; border-radius: 4px;
                padding: 6px 14px; font-size: 12px;
            }}
            QPushButton:hover {{
                background: {BORDER.name()}; border-color: {BORDER_STRONG.name()};
            }}
            QPushButton:pressed {{
                background: {BORDER_STRONG.name()};
            }}
        """

        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(btn_style)
        save_btn.clicked.connect(self._save_chain)
        t_layout.addWidget(save_btn)

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

        back_btn = QPushButton("← Back to Drone Bay")
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
        self.set_status("New workflow.", FG_MUTED)

    def _build_drone_lookup(self) -> dict[str, DroneDefinition]:
        drones = DroneStore.list_drones(self._workspace_root)
        return {d.id: d for d in drones}

    def _sync_form_from_chain(self) -> None:
        if self._chain_name_input:
            self._chain_name_input.blockSignals(True)
            self._chain_name_input.setText(self._chain_name)
            self._chain_name_input.blockSignals(False)
        if self._chain_desc_input:
            self._chain_desc_input.blockSignals(True)
            self._chain_desc_input.setPlainText(self._chain_description)
            self._chain_desc_input.blockSignals(False)
        if self._chain_enabled_check:
            self._chain_enabled_check.blockSignals(True)
            self._chain_enabled_check.setChecked(self._chain_enabled)
            self._chain_enabled_check.blockSignals(False)
        if self._chain_schedule_input:
            self._chain_schedule_input.blockSignals(True)
            self._chain_schedule_input.setText(self._chain_schedule)
            self._chain_schedule_input.blockSignals(False)

    def _sync_chain_from_form(self) -> None:
        if self._chain_name_input:
            self._chain_name = self._chain_name_input.text()
        if self._chain_desc_input:
            self._chain_description = self._chain_desc_input.toPlainText()
        if self._chain_enabled_check:
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
        self._auto_save_timer.start()
        # Update property panel on selection change
        self._property_panel.rebuild()
        self._update_left_panel_mode()

    def _on_chain_property_changed(self) -> None:
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
        back_btn = QPushButton("\u2190 Back to Drone Palette")
        back_btn.setFlat(True)
        back_btn.setCursor(Qt.PointingHandCursor)
        back_btn.setStyleSheet(f"QPushButton {{ color: {FG_MUTED.name()}; text-align: left; padding: 2px; }}")
        back_btn.clicked.connect(self._on_back_to_palette)
        layout.addWidget(back_btn)

        # Workshop header
        ws_header = QLabel("Drone Workshop")
        ws_header.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {FG.name()}; padding: 2px 0;")
        layout.addWidget(ws_header)

        # Draft info header
        self._ws_draft_label = QLabel()
        self._ws_draft_label.setWordWrap(True)
        self._ws_draft_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(self._ws_draft_label)

        self._ws_contracts_label = QLabel()
        self._ws_contracts_label.setWordWrap(True)
        self._ws_contracts_label.setStyleSheet(f"color: {FG_MUTED.name()}; font-size: 11px;")
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
        self._left_stack.addWidget(container)  # index 1
        return container

    def _update_left_panel_mode(self) -> None:
        selection = self._canvas._scene.selectedItems()
        draft_node = None
        for item in selection:
            if isinstance(item, ChainNodeItem) and item.is_draft:
                draft_node = item
                break

        if draft_node:
            # Save current palette width before expanding for workshop
            if self._left_stack.currentIndex() == 0:
                self._palette_width = self._splitter.sizes()[0]
            self._show_workshop_for_draft(draft_node)
            self._splitter.setSizes([340, self._splitter.sizes()[1], self._splitter.sizes()[2]])
        else:
            if self._left_stack.currentIndex() != 0:
                self._splitter.setSizes([self._palette_width, self._splitter.sizes()[1], self._splitter.sizes()[2]])
                self._left_stack.setCurrentIndex(0)

    def _show_workshop_for_draft(self, draft_node: ChainNodeItem) -> None:
        if draft_node.node_id not in self._canvas._nodes:
            return

        self._ensure_workshop_panel()

        # Reset workshop state when switching to a different draft
        if draft_node.node_id != self._workshop_draft_node_id:
            self._workshop_panel.reset_workshop_state()

        # Update header
        name = draft_node.draft_name or "Untitled Drone"
        accepts = draft_node.draft_accepts or "any"
        produces = draft_node.draft_produces or "any"
        self._workshop_draft_node_id = draft_node.node_id
        self._ws_draft_label.setText(f"Building: {name}")
        self._ws_contracts_label.setText(f"In: {accepts}  \u00b7  Out: {produces}")

        if self._left_stack.currentIndex() != 1:
            self._left_stack.setCurrentIndex(1)

    def _on_back_to_palette(self) -> None:
        # Deselect any draft node so palette comes back
        self._workshop_draft_node_id = None
        if self._workshop_panel is not None:
            self._workshop_panel.reset_workshop_state()
        self._canvas._scene.clearSelection()
        self._left_stack.setCurrentIndex(0)

    def _on_workshop_build_requested(self, brief) -> None:
        if self._workshop_draft_node_id is None:
            logger.warning("Workshop build requested but no draft node is active")
            return
        self.settle_draft_requested.emit({
            "brief": brief,
            "draft_node_id": self._workshop_draft_node_id,
        })

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

    # ---- Status bar ----

    def set_status(self, message: str, color: QColor = FG_MUTED) -> None:
        self._status_label.setText(message)
        self._status_label.setStyleSheet(f"""
            background: {BG_ALT.name()}; color: {color.name()};
            padding: 4px 8px; border-top: 1px solid {BORDER.name()};
        """)

    # ---- Workspace root ----

    def set_workspace_root(self, path: Path) -> None:
        self._workspace_root = path
        self._palette.set_workspace_root(path)
        self._load_or_create_chain(self._chain_id)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root
