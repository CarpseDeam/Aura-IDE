from __future__ import annotations

import math
import uuid
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QMenu,
)

from aura.drones.chain import ChainDefinition
from aura.drones.definition import DroneDefinition
from aura.gui.theme import (
    ACCENT,
    BG_ALT,
    DANGER,
    FG,
    FG_MUTED,
    SUCCESS,
    WARN,
)

NODE_WIDTH = 140
NODE_HEIGHT = 60
PORT_RADIUS = 4
PORT_DIAMETER = PORT_RADIUS * 2


class PortItem(QGraphicsItem):
    """Small circular port attached to a ChainNodeItem — input or output."""

    def __init__(self, parent_node: ChainNodeItem, is_input: bool):
        super().__init__(parent_node)
        self._parent_node = parent_node
        self._is_input = is_input
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False

    def boundingRect(self) -> QRectF:
        return QRectF(-PORT_RADIUS, -PORT_RADIUS, PORT_DIAMETER, PORT_DIAMETER)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        color = self._parent_node.border_color
        if self._hovered:
            color = ACCENT
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(color.darker(130), 1))
        painter.drawEllipse(self.boundingRect())

    def hoverEnterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    @property
    def parent_node(self) -> ChainNodeItem:
        return self._parent_node

    @property
    def is_input(self) -> bool:
        return self._is_input

    def center_scene(self) -> QPointF:
        """Return the port's center in scene coordinates."""
        return self.mapToScene(self.boundingRect().center())


class ChainNodeItem(QGraphicsObject):
    """A rounded-rect node on the canvas representing a drone in the workflow."""

    def __init__(
        self,
        node_id: str,
        drone: DroneDefinition,
        goal_template: str,
        canvas: ChainCanvas,
        is_draft: bool = False,
        draft_name: str = "",
        draft_accepts: str = "",
        draft_produces: str = "",
        draft_brief: str = "",
    ):
        super().__init__()
        self._node_id = node_id
        self._drone = drone
        self._goal_template = goal_template
        self._canvas = canvas
        self._missing = False
        self._is_draft = is_draft
        self._draft_name = draft_name
        self._draft_accepts = draft_accepts
        self._draft_produces = draft_produces
        self._draft_brief = draft_brief

        # Ports
        self.input_port = PortItem(self, is_input=True)
        self.output_port = PortItem(self, is_input=False)
        self._position_ports()

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._hovered = False

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def drone(self) -> DroneDefinition:
        return self._drone

    @property
    def drone_id(self) -> str:
        return self._drone.id if self._drone else "?"

    @property
    def goal_template(self) -> str:
        return self._goal_template

    @goal_template.setter
    def goal_template(self, value: str) -> None:
        self._goal_template = value

    @property
    def is_draft(self) -> bool:
        return self._is_draft

    @property
    def draft_name(self) -> str:
        return self._draft_name

    @draft_name.setter
    def draft_name(self, value: str) -> None:
        self._draft_name = value
        self.update()

    @property
    def draft_accepts(self) -> str:
        return self._draft_accepts

    @property
    def draft_produces(self) -> str:
        return self._draft_produces

    @property
    def draft_brief(self) -> str:
        return self._draft_brief

    @property
    def missing(self) -> bool:
        return self._missing

    @missing.setter
    def missing(self, value: bool) -> None:
        self._missing = value
        self.update()

    @property
    def border_color(self) -> QColor:
        if self._is_draft:
            return QColor("#9b8bb5")
        if self._missing:
            return DANGER
        policy = getattr(self._drone, "write_policy", "read_only")
        return SUCCESS if policy == "read_only" else WARN

    def _position_ports(self) -> None:
        """Place input/output ports on left and right edges."""
        self.input_port.setPos(0, NODE_HEIGHT / 2)
        self.output_port.setPos(NODE_WIDTH, NODE_HEIGHT / 2)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect().adjusted(1, 1, -1, -1)

        # Body
        painter.setBrush(QBrush(BG_ALT))
        border = self.border_color
        pen_w = 2
        if self.isSelected():
            border = ACCENT
            pen_w = 3
        painter.setPen(QPen(border, pen_w))
        painter.drawRoundedRect(rect, 6, 6)

        if self._is_draft:
            # Draft node: show draft name and inferred badges
            painter.setPen(QPen(FG))
            font = QFont()
            font.setBold(True)
            font.setPointSize(11)
            painter.setFont(font)
            name = self._draft_name or "Untitled Drone"
            fm = QFontMetrics(font)
            name = fm.elidedText(name, Qt.TextElideMode.ElideRight, NODE_WIDTH - 12)
            painter.drawText(QRectF(6, 4, NODE_WIDTH - 12, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

            font_small = QFont()
            font_small.setPointSize(8)
            painter.setFont(font_small)
            painter.setPen(QPen(FG_MUTED))
            painter.drawText(QRectF(6, 22, NODE_WIDTH - 12, 14),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             "Draft Drone")
            badge_y = 36
            painter.drawText(QRectF(6, badge_y, NODE_WIDTH // 2 - 8, 14),
                             Qt.AlignmentFlag.AlignLeft,
                             f"in: {self._draft_accepts or '?'}")
            painter.drawText(QRectF(NODE_WIDTH // 2 + 2, badge_y, NODE_WIDTH // 2 - 8, 14),
                             Qt.AlignmentFlag.AlignLeft,
                             f"out: {self._draft_produces or '?'}")
            painter.drawText(QRectF(6, 48, NODE_WIDTH - 12, 10),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             "Save before run")
            return

        # Drone name
        painter.setPen(QPen(FG))
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        painter.setFont(font)
        name = self._drone.name if self._drone else "(missing)"
        if self._missing:
            name += " (missing)"
        # Truncate if needed
        fm = QFontMetrics(font)
        name = fm.elidedText(name, Qt.TextElideMode.ElideRight, NODE_WIDTH - 12)
        painter.drawText(QRectF(6, 4, NODE_WIDTH - 12, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        # Badges
        font_small = QFont()
        font_small.setPointSize(8)
        painter.setFont(font_small)
        painter.setPen(QPen(FG_MUTED))
        accepts = getattr(self._drone, "accepts", None) or "any"
        produces = getattr(self._drone, "produces", None) or "any"
        badge_y = 24
        painter.drawText(QRectF(6, badge_y, NODE_WIDTH // 2 - 8, 14),
                         Qt.AlignmentFlag.AlignLeft, f"in: {accepts}")
        painter.drawText(QRectF(NODE_WIDTH // 2 + 2, badge_y, NODE_WIDTH // 2 - 8, 14),
                         Qt.AlignmentFlag.AlignLeft, f"out: {produces}")

        # Goal preview
        if self._goal_template:
            font_goal = QFont()
            font_goal.setPointSize(9)
            font_goal.setItalic(True)
            painter.setFont(font_goal)
            painter.setPen(QPen(FG_MUTED))
            preview = self._goal_template[:40]
            if len(self._goal_template) > 40:
                preview += "…"
            painter.drawText(QRectF(6, 38, NODE_WIDTH - 12, 18),
                             Qt.AlignmentFlag.AlignLeft, preview)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._canvas._on_node_moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._canvas._on_selection_changed()
        return super().itemChange(change, value)


class ChainEdgeItem(QGraphicsPathItem):
    """Bezier curve edge between two ports."""

    def __init__(self, source_port: PortItem, target_port: PortItem, canvas: ChainCanvas):
        super().__init__()
        self._source_port = source_port
        self._target_port = target_port
        self._canvas = canvas
        self._from_node_id = source_port.parent_node.node_id
        self._to_node_id = target_port.parent_node.node_id

        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self._hovered = False
        self._adjust()

    @property
    def from_node_id(self) -> str:
        return self._from_node_id

    @property
    def to_node_id(self) -> str:
        return self._to_node_id

    def _adjust(self) -> None:
        """Recalculate the bezier path to follow port positions."""
        if not self._source_port or not self._target_port:
            return
        p1 = self._source_port.center_scene()
        p4 = self._target_port.center_scene()
        dx = abs(p4.x() - p1.x())
        control_offset = max(50, dx * 0.5)
        p2 = QPointF(p1.x() + control_offset, p1.y())
        p3 = QPointF(p4.x() - control_offset, p4.y())

        path = self._build_bezier_path(p1, p2, p3, p4)
        self.setPath(path)

        # Update pen
        color = FG_MUTED
        if self.isSelected():
            color = ACCENT
        if self._hovered:
            color = ACCENT
        self.setPen(QPen(color, 2, Qt.PenStyle.SolidLine))

    def _build_bezier_path(self, p1, p2, p3, p4):
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.moveTo(p1)
        path.cubicTo(p2, p3, p4)
        # Arrow head
        arrow_size = 8
        angle = -path.angleAtPercent(1.0)
        arrow_p1 = p4 + QPointF(
            arrow_size * math.cos(math.radians(angle - 30)),
            arrow_size * math.sin(math.radians(angle - 30)),
        )
        arrow_p2 = p4 + QPointF(
            arrow_size * math.cos(math.radians(angle + 30)),
            arrow_size * math.sin(math.radians(angle + 30)),
        )
        path.moveTo(p4)
        path.lineTo(arrow_p1)
        path.moveTo(p4)
        path.lineTo(arrow_p2)
        return path

    def contextMenuEvent(self, event) -> None:
        menu = QMenu()
        insert_action = menu.addAction("Insert Drone Between")
        menu.addSeparator()
        delete_action = menu.addAction("Delete Edge")
        action = menu.exec(event.screenPos())
        if action == insert_action:
            self._canvas._canvas_insert_draft_between(self._source_port.parent_node, self._target_port.parent_node)
        elif action == delete_action:
            self._canvas._remove_edge(self)

    def hoverEnterEvent(self, event) -> None:
        self._hovered = True
        self._adjust()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hovered = False
        self._adjust()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._adjust()
        return super().itemChange(change, value)


class ChainCanvas(QGraphicsView):
    """QGraphicsView-based canvas for building drone workflow chains."""

    canvasChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(-2000, -2000, 4000, 4000, self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setInteractive(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAcceptDrops(True)

        # Drawing state
        self._nodes: dict[str, ChainNodeItem] = {}
        self._edges: list[ChainEdgeItem] = []
        self._drawing_source_port: PortItem | None = None
        self._rubber_band: QGraphicsLineItem | None = None
        self._drawing_cancelled = False

        # Empty state text
        self._empty_text: QGraphicsTextItem | None = None
        self._update_empty_text()

        # Space background
        self._space_bg_cache: QPixmap | None = None
        self._stars: list[tuple[float, float, float, float]] = []
        self._precompute_stars(400)
        self._space_offset_x = 0.0
        self._space_offset_y = 0.0
        self._space_timer = QTimer(self)
        self._space_timer.timeout.connect(self._on_space_tick)
        self._space_timer.start(100)

    def _update_empty_text(self) -> None:
        if self._empty_text:
            self._scene.removeItem(self._empty_text)
            self._empty_text = None
        if not self._nodes:
            text = self._scene.addText(
                "Drag drones here to build your workflow.",
                QFont("Segoe UI", 14),
            )
            text.setDefaultTextColor(FG_MUTED)
            text.setPos(-180, 0)
            self._empty_text = text

    def load_chain(self, chain: ChainDefinition, drone_lookup: dict[str, DroneDefinition]) -> None:
        """Populate canvas from a ChainDefinition."""
        self._scene.clear()
        self._nodes.clear()
        self._edges.clear()
        self._drawing_source_port = None
        self._rubber_band = None

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

        self._update_empty_text()

        # Auto-fit after a short delay
        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, self._fit_view)

    def _fit_view(self) -> None:
        if self._nodes:
            items_rect = self._scene.itemsBoundingRect()
            self.fitInView(items_rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)
        else:
            self.fitInView(QRectF(-300, -100, 600, 200))

    def to_chain_nodes_and_edges(self) -> tuple[list[dict], list[dict]]:
        """Snapshot current canvas state to serializable dicts."""
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
            })

        edges = []
        for edge in self._edges:
            edges.append({
                "from_node": edge.from_node_id,
                "to_node": edge.to_node_id,
            })

        return nodes, edges

    # ---- Edge drawing ----

    def _start_edge_draw(self, port: PortItem) -> None:
        self._drawing_source_port = port
        scene_pos = port.center_scene()
        self._rubber_band = self._scene.addLine(
            QLineF(scene_pos, scene_pos),
            QPen(ACCENT, 2, Qt.PenStyle.DashLine),
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
        """Zoom with Ctrl+scroll or plain scroll."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15
            if event.angleDelta().y() > 0:
                self.scale(factor, factor)
            else:
                self.scale(1 / factor, 1 / factor)
            event.accept()
        else:
            super().wheelEvent(event)

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
            scene_pos = self.mapToScene(event.position().toPoint())
            self._canvas_drop_drone(drone_id, scene_pos)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def _canvas_drop_drone(self, drone_id: str, scene_pos: QPointF) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        node_id = f"{drone_id}-{uuid.uuid4().hex[:4]}"
        item = ChainNodeItem(node_id=node_id, drone=drone, goal_template="", canvas=self)
        item.setPos(scene_pos - QPointF(NODE_WIDTH / 2, NODE_HEIGHT / 2))
        self._scene.addItem(item)
        self._nodes[node_id] = item
        self._scene.clearSelection()
        item.setSelected(True)
        self._update_empty_text()
        self.canvasChanged.emit()

    def _canvas_insert_draft_between(self, source_node: ChainNodeItem, target_node: ChainNodeItem) -> None:
        edge_to_remove = None
        for edge in self._edges:
            if edge.from_node_id == source_node.node_id and edge.to_node_id == target_node.node_id:
                edge_to_remove = edge
                break
        if edge_to_remove is None:
            return
        self._remove_edge(edge_to_remove)

        if source_node.drone is not None:
            draft_accepts = getattr(source_node.drone, "produces", "") or ""
        elif source_node.is_draft:
            draft_accepts = source_node.draft_produces or ""
        else:
            draft_accepts = ""

        if target_node.drone is not None:
            draft_produces = getattr(target_node.drone, "accepts", "") or ""
        elif target_node.is_draft:
            draft_produces = target_node.draft_accepts or ""
        else:
            draft_produces = ""

        src_center = source_node.pos() + QPointF(NODE_WIDTH / 2, NODE_HEIGHT / 2)
        tgt_center = target_node.pos() + QPointF(NODE_WIDTH / 2, NODE_HEIGHT / 2)
        center = QPointF((src_center.x() + tgt_center.x()) / 2, (src_center.y() + tgt_center.y()) / 2)
        draft_pos = center - QPointF(NODE_WIDTH / 2, NODE_HEIGHT / 2)

        node_id = f"draft-{uuid.uuid4().hex[:8]}"
        item = ChainNodeItem(
            node_id=node_id,
            drone=None,
            goal_template="",
            canvas=self,
            is_draft=True,
            draft_name="Untitled Drone",
            draft_accepts=draft_accepts,
            draft_produces=draft_produces,
        )
        item.setPos(draft_pos)
        self._scene.addItem(item)
        self._nodes[node_id] = item

        edge1 = ChainEdgeItem(source_port=source_node.output_port, target_port=item.input_port, canvas=self)
        self._scene.addItem(edge1)
        self._edges.append(edge1)

        edge2 = ChainEdgeItem(source_port=item.output_port, target_port=target_node.input_port, canvas=self)
        self._scene.addItem(edge2)
        self._edges.append(edge2)

        self._scene.clearSelection()
        item.setSelected(True)

        self._update_empty_text()
        self.canvasChanged.emit()

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
                     if isinstance(item, (ChainNodeItem, ChainEdgeItem))]
        for item in to_remove:
            if isinstance(item, ChainNodeItem):
                self._remove_node(item)
            elif isinstance(item, ChainEdgeItem):
                self._remove_edge(item)

    def _remove_node(self, node: ChainNodeItem) -> None:
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
        item_at_pos = self.itemAt(event.pos())
        if item_at_pos is None or isinstance(item_at_pos, QGraphicsTextItem):
            menu = QMenu()
            add_draft_action = menu.addAction("Create Drone Here")
            action = menu.exec(event.globalPos())
            if action == add_draft_action:
                self._canvas_add_draft_node(self.mapToScene(event.pos()))
            return
        super().contextMenuEvent(event)

    def _canvas_add_draft_node(self, scene_pos: QPointF) -> ChainNodeItem:
        node_id = f"draft-{uuid.uuid4().hex[:8]}"
        item = ChainNodeItem(
            node_id=node_id,
            drone=None,
            goal_template="",
            canvas=self,
            is_draft=True,
            draft_name="Untitled Drone",
        )
        item.setPos(scene_pos - QPointF(NODE_WIDTH / 2, NODE_HEIGHT / 2))
        self._scene.addItem(item)
        self._nodes[node_id] = item
        self._update_empty_text()
        self._scene.clearSelection()
        item.setSelected(True)
        self.canvasChanged.emit()
        return item

    # ---- Space background ----

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.save()
        painter.resetTransform()

        viewport_rect = self.viewport().rect()
        painter.fillRect(viewport_rect, QColor("#0a0a10"))

        cache = self._space_bg_cache
        if cache is None or cache.size() != viewport_rect.size():
            cache = self._build_space_cache(viewport_rect.size())
            self._space_bg_cache = cache
        painter.drawPixmap(viewport_rect, cache)

        visible_rect = self.mapToScene(viewport_rect).boundingRect()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for sx, sy, size, brightness in self._stars:
            wx = (sx + self._space_offset_x) % (visible_rect.width() + 200) + visible_rect.left() - 100
            wy = (sy + self._space_offset_y) % (visible_rect.height() + 200) + visible_rect.top() - 100
            if not visible_rect.contains(wx, wy):
                continue
            view_pos = self.mapFromScene(QPointF(wx, wy))
            alpha = int(40 + brightness * 200)
            painter.setBrush(QColor(200, 210, 255, alpha))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(view_pos, size, size)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.restore()

    def _precompute_stars(self, count: int = 400) -> None:
        import random
        rng = random.Random(42)
        for _ in range(count):
            x = rng.uniform(-4000, 4000)
            y = rng.uniform(-3000, 3000)
            size = rng.uniform(0.6, 2.2)
            brightness = rng.uniform(0.0, 1.0) ** 2
            self._stars.append((x, y, size, brightness))

    def _build_space_cache(self, size) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = size.width(), size.height()

        center_x, center_y = w / 2, h / 2
        max_dist = math.sqrt(center_x ** 2 + center_y ** 2)
        vignette_gradient = QRadialGradient(QPointF(center_x, center_y), max_dist * 0.75)
        vignette_gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        vignette_gradient.setColorAt(0.6, QColor(0, 0, 0, 40))
        vignette_gradient.setColorAt(1.0, QColor(0, 0, 0, 180))
        p.fillRect(0, 0, w, h, vignette_gradient)

        nebula_gradient = QLinearGradient(QPointF(0, h * 0.3), QPointF(w * 0.7, 0))
        nebula_gradient.setColorAt(0.0, QColor(90, 70, 160, 0))
        nebula_gradient.setColorAt(0.35, QColor(90, 70, 160, 15))
        nebula_gradient.setColorAt(0.55, QColor(110, 80, 180, 18))
        nebula_gradient.setColorAt(0.75, QColor(70, 60, 140, 8))
        nebula_gradient.setColorAt(1.0, QColor(50, 45, 120, 0))
        p.fillRect(0, 0, w, h, nebula_gradient)

        nebula2 = QLinearGradient(QPointF(w * 0.6, h * 0.7), QPointF(w * 0.2, h))
        nebula2.setColorAt(0.0, QColor(80, 60, 150, 0))
        nebula2.setColorAt(0.5, QColor(100, 75, 170, 10))
        nebula2.setColorAt(1.0, QColor(60, 50, 130, 0))
        p.fillRect(0, 0, w, h, nebula2)

        p.end()
        return pixmap

    def _on_space_tick(self) -> None:
        if not self.isVisible():
            return
        self._space_offset_x += 0.15
        self._space_offset_y += 0.08
        if self._stars:
            self.viewport().update()
