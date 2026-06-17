from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QMenu,
)

from aura.drones.chain import ChainDefinition
from aura.drones.definition import DroneDefinition
from aura.gui.theme import (
    ACCENT,
    DANGER,
    FG_MUTED,
)

from aura.gui.drones.chain_canvas_items import PortItem, ChainEdgeItem
from aura.gui.drones.chain_node_item import ChainNodeItem
from aura.gui.drones.mission_core_item import MissionCoreItem
from aura.gui.drones.goal_planet_item import GoalPlanetItem

logger = logging.getLogger(__name__)

PORT_RADIUS = 3
PORT_DIAMETER = PORT_RADIUS * 2
MISSION_CORE_WIDTH = 240
MISSION_CORE_HEIGHT = 120
ASSIGNMENT_HEIGHT = 24


def _qt_color(value, fallback="#ffffff"):
    """Return a QColor from a string token or QColor, falling back on invalid."""
    if isinstance(value, QColor):
        return value
    color = QColor(str(value))
    if not color.isValid():
        color = QColor(fallback)
    return color


def _is_valid_item(item) -> bool:
    """Return True if the PySide6 C++ object backing item is still alive."""
    if item is None:
        return False
    try:
        import shiboken6
        return shiboken6.isValid(item)
    except Exception:
        # shiboken6 unavailable — fall back to scene membership as a proxy
        try:
            return item.scene() is not None
        except Exception:
            return False


def _populate_planet_from_drone(drone: DroneDefinition) -> tuple[str, str]:
    """Derive a deterministic (title, objective) pair from a DroneDefinition."""
    if drone.name:
        title = f"{drone.name} Target"
    else:
        desc_words = (drone.description or "").split()
        if desc_words:
            title = " ".join(desc_words[:5])
        else:
            title = "Mission Goal"

    if drone.description:
        objective = drone.description
    elif drone.output_contract and drone.output_contract.get("description"):
        objective = drone.output_contract["description"]
    elif drone.instructions:
        objective = drone.instructions
    elif drone.name:
        objective = f"Complete the mission for {drone.name}"
    else:
        objective = "Complete the assigned task"

    return title, objective



class ChainCanvas(QGraphicsView):
    """QGraphicsView-based canvas for building drone workflow chains."""

    canvasChanged = Signal()
    runMissionRequested = Signal()
    statusMessage = Signal(str, str)  # (text, level)
    renameWorkflowRequested = Signal()

    def __init__(self, parent=None):
        # Set all attributes before super().__init__() because Qt paint events
        # (drawForeground / drawBackground) fire during construction and will
        # AttributeError if these are missing.
        self._nodes: dict[str, ChainNodeItem] = {}
        self._edges: list[ChainEdgeItem] = []
        self._drawing_source_port: PortItem | None = None
        self._rubber_band: QGraphicsLineItem | None = None
        self._drawing_cancelled = False
        self._empty_text: QGraphicsTextItem | None = None
        self._mission_core: MissionCoreItem | None = None
        self._goal_planets: dict[str, GoalPlanetItem] = {}
        self._space_bg_cache: QPixmap | None = None
        self._last_click_time: float = 0.0
        self._last_click_pos: QPointF = QPointF()

        super().__init__(parent)

        self._scene = QGraphicsScene(-2000, -2000, 4000, 4000, self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setInteractive(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAcceptDrops(True)

    def _update_empty_text(self) -> None:
        if self._empty_text is not None:
            try:
                self._scene.removeItem(self._empty_text)
            except RuntimeError:
                pass  # C++ object already deleted
            self._empty_text = None

        if not self._nodes and self._mission_core is None and not self._goal_planets:
            text = QGraphicsTextItem(
                "Right-click to add a Mission Core and Goal Planets."
            )
            font = QFont()
            font.setPixelSize(11)
            text.setFont(font)
            text.setDefaultTextColor(_qt_color(FG_MUTED))
            text.setPos(-120, -15)
            self._scene.addItem(text)
            self._empty_text = text


    def load_chain(self, chain: ChainDefinition, drone_lookup: dict[str, DroneDefinition], mission_core_data: dict | None = None, goal_planets_data: list[dict] | None = None) -> None:
        """Populate canvas from a ChainDefinition."""
        self._scene.clear()
        self._empty_text = None  # scene.clear() deleted the C++ object
        self._nodes.clear()
        self._edges.clear()
        self._drawing_source_port = None
        self._rubber_band = None
        self._mission_core = None
        self._goal_planets.clear()

        for node_data in chain.nodes:
            drone_id = node_data.drone_id
            drone = drone_lookup.get(drone_id)
            item = ChainNodeItem(
                node_id=node_data.id,
                drone=drone,
                goal_template=node_data.goal_template,
                canvas=self,
                is_draft=node_data.is_draft,
                draft_name=node_data.draft_name,
                draft_accepts=node_data.draft_accepts,
                draft_produces=node_data.draft_produces,
                draft_brief=node_data.draft_brief,
                is_assignment=node_data.is_assignment,
                goal_id=node_data.goal_id,
            )
            if drone is None and not node_data.is_draft:
                item.missing = True
            if node_data.position and len(node_data.position) == 2:
                item.setPos(node_data.position[0], node_data.position[1])
            self._scene.addItem(item)
            self._nodes[node_data.id] = item

        for edge_data in chain.edges:
            src = self._nodes.get(edge_data.from_node)
            tgt = self._nodes.get(edge_data.to_node)
            if src and tgt:
                edge = ChainEdgeItem(
                    source_port=src.output_port,
                    target_port=tgt.input_port,
                    canvas=self,
                )
                self._scene.addItem(edge)
                self._edges.append(edge)

        # Mission core
        self._mission_core = None
        if mission_core_data:
            mission_item = MissionCoreItem(
                node_id=f"mission-core-{uuid.uuid4().hex[:4]}",
                canvas=self,
            )
            mission_item.from_dict(mission_core_data)
            self._scene.addItem(mission_item)
            self._mission_core = mission_item
            mission_item.runRequested.connect(self.runMissionRequested.emit)

        # Goal planets — iterate chain.goals, apply matching data from goal_planets_data
        goals_data_by_id: dict[str, dict] = {}
        if goal_planets_data:
            for gd in goal_planets_data:
                gid = gd.get("id", gd.get("goal_id", ""))
                if gid:
                    goals_data_by_id[gid] = gd

        for goal in chain.goals:
            gp_item = GoalPlanetItem(
                node_id=f"goal-planet-{uuid.uuid4().hex[:4]}",
                canvas=self,
                goal_id=goal.id,
            )
            if goal.position and len(goal.position) == 2:
                gp_item.setPos(goal.position[0], goal.position[1])
            gp_item.objective = goal.objective
            gp_item.title = goal.title

            # Apply any saved canvas data for this goal (e.g. seed, style, position override)
            match_data = goals_data_by_id.get(goal.id)
            if match_data:
                gp_item.from_dict(match_data)
            self._scene.addItem(gp_item)
            self._goal_planets[goal.id] = gp_item

        self._update_empty_text()

        # Auto-create default mission core for empty chains
        if self._mission_core is None and not self._goal_planets and not self._nodes:
            self._canvas_add_mission_core(QPointF(-160, 0))

        # Auto-fit after a short delay
        QTimer.singleShot(100, self._fit_view)

    def _fit_view(self) -> None:
        node_count = len(self._nodes)
        if node_count == 0 and self._mission_core is None and not self._goal_planets:
            self.resetTransform()
            self.centerOn(0, 0)
        elif node_count == 1 and self._mission_core is None and not self._goal_planets:
            self.resetTransform()
            node = next(iter(self._nodes.values()))
            self.centerOn(node.sceneBoundingRect().center())
        elif node_count == 0 and self._mission_core is not None:
            self.resetTransform()
            self.centerOn(self._mission_core.sceneBoundingRect().center())
        elif node_count == 0 and self._mission_core is None and self._goal_planets:
            first_planet = next(iter(self._goal_planets.values()))
            self.resetTransform()
            self.centerOn(first_planet.sceneBoundingRect().center())
        else:
            items_rect = self._scene.itemsBoundingRect()
            self.fitInView(items_rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)

        # Clamp minimum zoom so canvas doesn't zoom out too far
        current_scale = self.transform().m11()
        if current_scale < 0.35:
            factor = 0.35 / current_scale
            self.scale(factor, factor)

    def to_chain_nodes_and_edges(self) -> tuple[list[dict], list[dict], dict | None, list[dict]]:
        """Snapshot current canvas state to serializable dicts.

        Returns (nodes, edges, mission_core_dict, goals_list).
        """
        nodes = []
        for node_id, item in self._nodes.items():
            pos = item.pos()
            nodes.append({
                "id": node_id,
                "drone_id": item.drone_id,
                "goal_template": item.goal_template,
                "position": [pos.x(), pos.y()],
                "is_draft": item.is_draft,
                "draft_name": item.draft_name,
                "draft_accepts": item.draft_accepts,
                "draft_produces": item.draft_produces,
                "draft_brief": item.draft_brief,
                "is_assignment": item.is_assignment,
                "goal_id": item.goal_id,
            })

        edges = []
        for edge in self._edges:
            edges.append({
                "from_node": edge.from_node_id,
                "to_node": edge.to_node_id,
            })

        mission_dict = self._mission_core.to_dict() if self._mission_core else None

        goals = []
        for gp in self._goal_planets.values():
            goals.append(gp.to_dict())

        return nodes, edges, mission_dict, goals

    @property
    def goal_planets_data(self) -> list[dict]:
        return [gp.to_dict() for gp in self._goal_planets.values()]

    # ---- Edge drawing ----

    def _start_edge_draw(self, port: PortItem) -> None:
        self._drawing_source_port = port
        scene_pos = port.center_scene()
        self._rubber_band = self._scene.addLine(
            QLineF(scene_pos, scene_pos),
            QPen(_qt_color(ACCENT), 2, Qt.PenStyle.DashLine),
        )
        # Set mouse tracking so we get move events
        self.setMouseTracking(True)

    def _update_rubber_band(self, scene_pos: QPointF) -> None:
        if self._rubber_band and self._drawing_source_port:
            src = self._drawing_source_port.center_scene()
            self._rubber_band.setLine(QLineF(src, scene_pos))

    def _complete_edge(self, target_port: PortItem) -> None:
        if not self._drawing_source_port:
            return
        src_node = self._drawing_source_port.parent_node
        tgt_node = target_port.parent_node
        if src_node == tgt_node:
            self._cancel_edge_draw()
            return
        # Check for duplicate edge
        for edge in self._edges:
            if edge.from_node_id == src_node.node_id and edge.to_node_id == tgt_node.node_id:
                self._cancel_edge_draw()
                return
        edge = ChainEdgeItem(
            source_port=self._drawing_source_port,
            target_port=target_port,
            canvas=self,
        )
        self._scene.addItem(edge)
        self._edges.append(edge)
        self._cleanup_edge_draw()
        self.canvasChanged.emit()

    def _cancel_edge_draw(self) -> None:
        self._cleanup_edge_draw()

    def _cleanup_edge_draw(self) -> None:
        if self._rubber_band:
            self._scene.removeItem(self._rubber_band)
            self._rubber_band = None
        self._drawing_source_port = None
        self.setMouseTracking(False)

    def _find_output_port(self, items: list[QGraphicsItem]) -> PortItem | None:
        for item in items:
            if isinstance(item, PortItem) and not item.is_input:
                return item
        return None

    def _find_input_port(self, items: list[QGraphicsItem]) -> PortItem | None:
        for item in items:
            if isinstance(item, PortItem) and item.is_input:
                return item
        return None

    # ---- Event overrides ----

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())

            # Double-click on empty canvas → fit view
            now = time.monotonic()
            dx = abs(scene_pos.x() - self._last_click_pos.x())
            dy = abs(scene_pos.y() - self._last_click_pos.y())
            if (now - self._last_click_time < 0.4 and dx < 15 and dy < 15):
                items = self._scene.items(scene_pos)
                if not items or all(isinstance(i, QGraphicsTextItem) for i in items):
                    self._fit_view()
                    self._last_click_time = 0.0
                    event.accept()
                    return
            self._last_click_time = now
            self._last_click_pos = scene_pos

            items = self._scene.items(scene_pos)
            output_port = self._find_output_port(items)
            if output_port is not None:
                self._start_edge_draw(output_port)
                event.accept()
                return
            # Also check if clicking on input port while drawing
            if self._drawing_source_port is not None:
                input_port = self._find_input_port(items)
                if input_port is not None and input_port.parent_node != self._drawing_source_port.parent_node:
                    self._complete_edge(input_port)
                    event.accept()
                    return
                elif input_port is not None:
                    self._cancel_edge_draw()
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drawing_source_port is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            self._update_rubber_band(scene_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drawing_source_port is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            items = self._scene.items(scene_pos)
            input_port = self._find_input_port(items)
            if input_port is not None and input_port.parent_node != self._drawing_source_port.parent_node:
                self._complete_edge(input_port)
            else:
                self._cancel_edge_draw()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        """Zoom around cursor with plain scroll wheel, clamped to safe range."""
        factor = 1.12
        if event.angleDelta().y() > 0:
            s = factor
        else:
            s = 1 / factor

        current = self.transform().m11()
        new_scale = current * s
        if new_scale < 0.15:
            s = 0.15 / current
        elif new_scale > 4.0:
            s = 4.0 / current

        self.scale(s, s)
        event.accept()

    # ---- Drag & drop from palette ----

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            drone_id = bytes(event.mimeData().data("application/x-aura-drone-id")).decode("utf-8")
            # Route background canvas drop to mission core if available
            if self._mission_core is None:
                scene_pos = self.mapToScene(event.position().toPoint())
                self._canvas_add_mission_core(scene_pos)
            self._handle_mission_core_drop(self._mission_core, drone_id)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def _get_workspace_root(self) -> Path:
        """Walk up parents to find ChainEditor's workspace_root."""
        p = self.parent()
        while p is not None:
            ws = getattr(p, "workspace_root", None)
            if ws:
                return ws
            p = p.parent()
        return Path.cwd()

    # ---- Deletions ----

    def _delete_selected(self) -> None:
        to_remove = [item for item in self._scene.selectedItems()
                     if isinstance(item, (ChainNodeItem, ChainEdgeItem, MissionCoreItem, GoalPlanetItem))]
        for item in to_remove:
            if isinstance(item, GoalPlanetItem):
                removed_id = item.goal_id
                if removed_id in self._goal_planets:
                    del self._goal_planets[removed_id]
                # Reassign any assignment nodes targeting this goal
                remaining_goals = list(self._goal_planets.keys())
                for node in self._nodes.values():
                    if node.is_assignment and node.goal_id == removed_id:
                        node.goal_id = remaining_goals[0] if remaining_goals else ""
                self._scene.removeItem(item)
                self._update_empty_text()
                self.canvasChanged.emit()
            elif isinstance(item, MissionCoreItem):
                self._mission_core = None
                self._scene.removeItem(item)
                self._update_empty_text()
                self.canvasChanged.emit()
            elif isinstance(item, ChainNodeItem):
                self._remove_node(item)
            elif isinstance(item, ChainEdgeItem):
                self._remove_edge(item)

    def _remove_node(self, node: ChainNodeItem) -> None:
        # Clean up mission core assignment
        if self._mission_core is not None:
            self._mission_core.remove_assigned_drone(node.drone_id)
        # Remove incident edges
        incident = [
            e for e in self._edges
            if e.from_node_id == node.node_id or e.to_node_id == node.node_id
        ]
        for edge in incident:
            self._remove_edge(edge)
        if node.node_id in self._nodes:
            del self._nodes[node.node_id]
        self._scene.removeItem(node)
        self._update_empty_text()
        self.canvasChanged.emit()

    def _remove_edge(self, edge: ChainEdgeItem) -> None:
        if edge in self._edges:
            self._edges.remove(edge)
        self._scene.removeItem(edge)
        self.canvasChanged.emit()

    # ---- Node callbacks ----

    def _on_node_moved(self) -> None:
        for edge in self._edges:
            edge._adjust()
        self.canvasChanged.emit()

    def _on_selection_changed(self) -> None:
        # Re-paint for selection state
        self.viewport().update()
        self.canvasChanged.emit()

    def contextMenuEvent(self, event) -> None:
        scene_pos = self.mapToScene(event.pos())
        items = self._scene.items(scene_pos)
        if not items or all(isinstance(i, (QGraphicsTextItem,)) for i in items):
            menu = QMenu()
            add_mission_action = menu.addAction("Add Mission Core")
            if self._mission_core is not None:
                add_mission_action.setEnabled(False)
            add_goal_action = menu.addAction("Add Goal Planet")
            menu.addSeparator()
            rename_action = menu.addAction("Rename Workflow")
            action = menu.exec(event.globalPos())
            if action == add_mission_action:
                self._canvas_add_mission_core(scene_pos)
            elif action == add_goal_action:
                self._canvas_add_goal_planet(scene_pos)
            elif action == rename_action:
                self.renameWorkflowRequested.emit()
            return
        super().contextMenuEvent(event)

    # ---- Mission core ----

    def _canvas_add_mission_core(self, scene_pos: QPointF) -> None:
        if self._mission_core is not None:
            return
        node_id = f"mission-core-{uuid.uuid4().hex[:4]}"
        item = MissionCoreItem(node_id=node_id, canvas=self)
        item.setPos(scene_pos)
        self._scene.addItem(item)
        self._mission_core = item
        item.runRequested.connect(self.runMissionRequested.emit)
        self._update_empty_text()
        self.canvasChanged.emit()

    def _canvas_add_goal_planet(self, scene_pos: QPointF) -> None:
        self._canvas_add_goal_planet_with_data(scene_pos)

    def _canvas_add_goal_planet_with_data(self, scene_pos: QPointF, title: str = "", objective: str = "") -> GoalPlanetItem:
        goal_id = f"goal-{uuid.uuid4().hex[:6]}"
        gp_item = GoalPlanetItem(
            node_id=f"goal-planet-{uuid.uuid4().hex[:4]}",
            canvas=self,
            goal_id=goal_id,
        )
        gp_item.setPos(scene_pos)
        if title:
            gp_item.title = title
        if objective:
            gp_item.objective = objective
        self._scene.addItem(gp_item)
        self._goal_planets[goal_id] = gp_item
        self._update_empty_text()
        self.canvasChanged.emit()
        return gp_item
    def _create_goal_planet_for_drone(self, drone: DroneDefinition) -> GoalPlanetItem:
        title, objective = _populate_planet_from_drone(drone)
        if self._mission_core is not None:
            mc_pos = self._mission_core.pos()
            base_x = mc_pos.x() + 200
            base_y = mc_pos.y() - 40
        else:
            base_x = 160
            base_y = 0
        scene_pos = QPointF(base_x, base_y + len(self._goal_planets) * 100)
        return self._canvas_add_goal_planet_with_data(scene_pos, title, objective)

    def _ensure_goal_for_drone(self, drone: DroneDefinition, preferred_goal_id: str = "") -> str:
        if preferred_goal_id and preferred_goal_id in self._goal_planets:
            return preferred_goal_id
        selected = [gp for gp in self._goal_planets.values() if gp.isSelected()]
        if len(selected) == 1:
            return selected[0].goal_id
        if len(self._goal_planets) == 1:
            return next(iter(self._goal_planets))
        return self._create_goal_planet_for_drone(drone).goal_id

    def _create_drone_assignment(self, drone: DroneDefinition, goal_id: str) -> ChainNodeItem:
        if self._mission_core is None:
            self._canvas_add_mission_core(QPointF(-160, 0))
        node_id = f"{drone.id}-{uuid.uuid4().hex[:4]}"
        item = ChainNodeItem(
            node_id=node_id,
            drone=drone,
            goal_template="",
            canvas=self,
            is_assignment=True,
            goal_id=goal_id,
        )
        mc = self._mission_core
        mc_pos = mc.pos()
        assignment_index = sum(1 for n in self._nodes.values() if n.is_assignment)
        x = mc_pos.x() + MISSION_CORE_WIDTH / 2 + 4
        y = mc_pos.y() - MISSION_CORE_HEIGHT / 2 + 6 + assignment_index * (ASSIGNMENT_HEIGHT + 4)
        item.setPos(x, y)
        self._scene.addItem(item)
        self._nodes[node_id] = item
        mc.add_assigned_drone(drone.id)
        self._scene.clearSelection()
        item.setSelected(True)
        self._update_empty_text()
        self.canvasChanged.emit()
        return item

    def _handle_mission_core_drop(self, mission_item: MissionCoreItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        goal_id = self._ensure_goal_for_drone(drone, "")
        self._create_drone_assignment(drone, goal_id)

    def _handle_goal_planet_drop(self, planet_item: GoalPlanetItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        self._create_drone_assignment(drone, planet_item.goal_id)

    # ---- Space background ----

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.save()
        painter.resetTransform()

        viewport_rect = self.viewport().rect()
        painter.fillRect(viewport_rect, QColor("#060608"))

        cache = self._space_bg_cache
        if cache is None or cache.size() != viewport_rect.size():
            cache = self._build_space_cache(viewport_rect.size())
            self._space_bg_cache = cache
        painter.drawPixmap(viewport_rect.topLeft(), cache)
        painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """Draw assignment connection lines from Goal Planets to their assignments."""
        try:
            line_color = _qt_color(ACCENT)
            line_color.setAlpha(25)
            pen = QPen(line_color)
            pen.setWidthF(0.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            for gp in list(self._goal_planets.values()):
                if not _is_valid_item(gp):
                    continue
                if gp.scene() is None:
                    continue
                gp_rect = gp.sceneBoundingRect()
                source_pt = QPointF(gp_rect.right(), gp_rect.center().y())

                for node in list(self._nodes.values()):
                    if not _is_valid_item(node):
                        continue
                    if node.scene() is None:
                        continue
                    if not node.is_assignment:
                        continue
                    if node.goal_id != gp.goal_id:
                        continue
                    node_rect = node.sceneBoundingRect()
                    target_pt = QPointF(node_rect.left(), node_rect.center().y())
                    painter.drawLine(source_pt, target_pt)

            # Fallback: assignments with no goal get a faint line from Mothership
            mc = self._mission_core
            if mc and _is_valid_item(mc) and mc.scene() is not None:
                mc_rect = mc.sceneBoundingRect()
                fallback_color = _qt_color(FG_MUTED)
                fallback_color.setAlpha(15)
                fallback_pen = QPen(fallback_color)
                fallback_pen.setWidthF(0.3)
                fallback_pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(fallback_pen)
                for node in list(self._nodes.values()):
                    if not _is_valid_item(node) or node.scene() is None:
                        continue
                    if not node.is_assignment:
                        continue
                    if node.goal_id:
                        continue
                    source_pt = QPointF(mc_rect.right(), mc_rect.center().y())
                    node_rect = node.sceneBoundingRect()
                    target_pt = QPointF(node_rect.left(), node_rect.center().y())
                    painter.drawLine(source_pt, target_pt)
        except Exception:
            logger.exception("drawForeground error — suppressed to protect Qt paint cycle")

    def _build_space_cache(self, size) -> QPixmap:
        import random
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = size.width(), size.height()

        # Radial gas clouds (layered additive)
        # Violet main bloom
        g1 = QRadialGradient(QPointF(0.30 * w, 0.58 * h), 0.46 * w)
        g1.setColorAt(0.0, QColor(157, 124, 216, 72))
        g1.setColorAt(0.45, QColor(150, 115, 205, 34))
        g1.setColorAt(1.0, QColor(157, 124, 216, 0))
        p.fillRect(0, 0, w, h, g1)

        # Warm magenta pocket
        g2 = QRadialGradient(QPointF(0.52 * w, 0.68 * h), 0.40 * w)
        g2.setColorAt(0.0, QColor(247, 118, 142, 55))
        g2.setColorAt(0.5, QColor(205, 95, 140, 24))
        g2.setColorAt(1.0, QColor(247, 118, 142, 0))
        p.fillRect(0, 0, w, h, g2)

        # Blue upper drift
        g3 = QRadialGradient(QPointF(0.62 * w, 0.34 * h), 0.42 * w)
        g3.setColorAt(0.0, QColor(122, 162, 247, 40))
        g3.setColorAt(0.5, QColor(110, 140, 220, 18))
        g3.setColorAt(1.0, QColor(122, 162, 247, 0))
        p.fillRect(0, 0, w, h, g3)

        # Cyan accent
        g4 = QRadialGradient(QPointF(0.80 * w, 0.55 * h), 0.28 * w)
        g4.setColorAt(0.0, QColor(125, 207, 255, 28))
        g4.setColorAt(1.0, QColor(125, 207, 255, 0))
        p.fillRect(0, 0, w, h, g4)

        # Subtle static stars
        rng = random.Random(42)
        for _ in range(250):
            sx = rng.uniform(0, w)
            sy = rng.uniform(0, h)
            sr = rng.uniform(0.4, 1.5)
            sa = rng.randint(20, 80)
            c = QColor(200, 208, 240, sa)
            p.setBrush(QBrush(c))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(sx, sy), sr, sr)

        p.end()
        return pixmap
