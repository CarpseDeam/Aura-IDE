from __future__ import annotations

import math
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
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
    DANGER,
    FG_MUTED,
)

NODE_WIDTH = 252
NODE_HEIGHT = 76
PORT_RADIUS = 3
PORT_DIAMETER = PORT_RADIUS * 2
NODE_RADIUS = 12


def _qt_color(value, fallback="#ffffff"):
    """Return a QColor from a string token or QColor, falling back on invalid."""
    if isinstance(value, QColor):
        return value
    color = QColor(str(value))
    if not color.isValid():
        color = QColor(fallback)
    return color


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
        color = QColor(self._parent_node.border_color)
        if self._hovered:
            color = _qt_color(ACCENT)
        else:
            color.setAlpha(130)
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(color.darker(150), 0.5))
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
            return QColor("#9d7cd8")
        if self._missing:
            return _qt_color(DANGER)
        policy = getattr(self._drone, "write_policy", "read_only")
        return QColor("#7dcfff") if policy == "read_only" else QColor("#e0af68")

    def _position_ports(self) -> None:
        """Place input/output ports on left and right edges."""
        self.input_port.setPos(0, NODE_HEIGHT / 2)
        self.output_port.setPos(NODE_WIDTH, NODE_HEIGHT / 2)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()

        # --- Card body: flat dark glass fill ---
        painter.setBrush(QBrush(QColor(18, 20, 28, 230)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, NODE_RADIUS, NODE_RADIUS)

        # --- Border / glow (single stroke) ---
        border_color = self.border_color
        if self.isSelected():
            border_color = _qt_color(ACCENT)

        base_alpha = 90
        if self._hovered:
            base_alpha = min(base_alpha + 30, 255)
        if self.isSelected():
            base_alpha = 170

        adjusted = rect.adjusted(1, 1, -1, -1)
        adj_radius = NODE_RADIUS - 1
        border_style = Qt.PenStyle.DashLine if self._is_draft else Qt.PenStyle.SolidLine
        pen_w = 2 if self.isSelected() else 1.5

        glow = QColor(border_color)
        glow.setAlpha(base_alpha)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(glow, pen_w, border_style))
        painter.drawRoundedRect(adjusted, adj_radius, adj_radius)

        # --- Row 1: status dot + title ---
        dot_color = QColor(border_color)
        dot_color.setAlpha(220)
        painter.setBrush(QBrush(dot_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(18, 22), 5, 5)

        painter.setPen(QPen(QColor("#eaecef")))
        font = QFont()
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)

        if self._is_draft:
            name = self._draft_name or "Untitled Drone"
        elif self._drone:
            name = self._drone.name
            if self._missing:
                name += " (missing)"
        else:
            name = "Missing Drone"

        fm = QFontMetrics(font)
        avail_w = NODE_WIDTH - 34 - 14
        name = fm.elidedText(name, Qt.TextElideMode.ElideRight, avail_w)
        painter.drawText(QRectF(34, 11, avail_w, 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        # --- Row 2: status pill + preview ---
        pill_x = 34
        pill_y = 42
        pill_h = 14

        if self._is_draft:
            pill_text = "draft"
            pill_color = QColor("#9d7cd8")
        elif self._missing:
            pill_text = "missing"
            pill_color = _qt_color(DANGER)
        else:
            policy = getattr(self._drone, "write_policy", "read_only")
            if policy == "read_only":
                pill_text = "read-only"
                pill_color = QColor("#7dcfff")
            else:
                pill_text = "writes"
                pill_color = QColor("#e0af68")

        font_pill = QFont()
        font_pill.setPixelSize(10)
        painter.setFont(font_pill)
        fm_pill = QFontMetrics(font_pill)
        pill_text_w = fm_pill.horizontalAdvance(pill_text) + 10
        pill_w = max(pill_text_w, 40)

        pill_bg = QColor(255, 255, 255, 13)
        painter.setBrush(QBrush(pill_bg))
        painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
        pill_rect = QRectF(pill_x, pill_y, pill_w, pill_h)
        painter.drawRoundedRect(pill_rect, 3, 3)

        painter.setPen(QPen(pill_color))
        painter.drawText(pill_rect.adjusted(4, 0, -4, 0),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, pill_text)

        # Preview text
        if self._is_draft:
            preview = self._draft_brief or ""
        elif self._goal_template:
            preview = self._goal_template
        elif self._drone:
            preview = self._drone.description or ""
        else:
            preview = ""

        if preview:
            preview_x = pill_x + pill_w + 6
            preview_w_avail = NODE_WIDTH - preview_x - 14
            if preview_w_avail > 20:
                font_pv = QFont()
                font_pv.setPixelSize(11)
                painter.setFont(font_pv)
                fm_pv = QFontMetrics(font_pv)
                preview = fm_pv.elidedText(preview, Qt.TextElideMode.ElideRight, int(preview_w_avail))
                painter.setPen(QPen(_qt_color(FG_MUTED)))
                painter.drawText(QRectF(preview_x, pill_y, preview_w_avail, pill_h),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, preview)

    def hoverEnterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

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
        self._bezier_curve: QPainterPath | None = None
        self._glow_color = QColor("#8b9eeb")
        self._pen_style = Qt.PenStyle.SolidLine
        self._adjust()

    @property
    def from_node_id(self) -> str:
        return self._from_node_id

    @property
    def to_node_id(self) -> str:
        return self._to_node_id

    def paint(self, painter: QPainter, option, widget=None) -> None:
        # Draw glow strokes behind the main path
        if self._bezier_curve is not None:
            for w, alpha in [(6, 15), (3, 30)]:
                c = QColor(self._glow_color)
                c.setAlpha(alpha)
                painter.setPen(QPen(c, w, self._pen_style))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(self._bezier_curve)
        super().paint(painter, option, widget)

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

        # Store bezier-only curve for glow painting
        self._bezier_curve = self._build_bezier_curve(p1, p2, p3, p4)

        # Determine lane color and style
        src_draft = self._source_port.parent_node.is_draft
        tgt_draft = self._target_port.parent_node.is_draft
        is_draft_edge = src_draft or tgt_draft

        if is_draft_edge:
            self._glow_color = QColor("#9d7cd8")
            self._pen_style = Qt.PenStyle.DashLine
        else:
            self._glow_color = QColor("#8b9eeb")
            self._pen_style = Qt.PenStyle.SolidLine

        if self.isSelected():
            self._glow_color = _qt_color(ACCENT)
        if self._hovered:
            self._glow_color = _qt_color(ACCENT)

        # Main pen
        main_color = self._glow_color
        main_width = 1.5 if not self.isSelected() else 2.5
        self.setPen(QPen(main_color, main_width, self._pen_style))

    def _build_bezier_curve(self, p1, p2, p3, p4) -> QPainterPath:
        path = QPainterPath()
        path.moveTo(p1)
        path.cubicTo(p2, p3, p4)
        return path

    def _build_bezier_path(self, p1, p2, p3, p4):
        path = self._build_bezier_curve(p1, p2, p3, p4)
        arrow_size = 7
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
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAcceptDrops(True)

        # Drawing state
        self._nodes: dict[str, ChainNodeItem] = {}
        self._edges: list[ChainEdgeItem] = []
        self._drawing_source_port: PortItem | None = None
        self._rubber_band: QGraphicsLineItem | None = None
        self._drawing_cancelled = False

        # Empty state text (created lazily by load_chain / _update_empty_text)
        self._empty_text: QGraphicsTextItem | None = None

        # Space background (static cached pixmap)
        self._space_bg_cache: QPixmap | None = None

        # Double-click detection for fit-view
        self._last_click_time: float = 0.0
        self._last_click_pos: QPointF = QPointF()

    def _update_empty_text(self) -> None:
        if self._empty_text is not None:
            try:
                self._scene.removeItem(self._empty_text)
            except RuntimeError:
                pass  # C++ object already deleted
            self._empty_text = None


    def load_chain(self, chain: ChainDefinition, drone_lookup: dict[str, DroneDefinition]) -> None:
        """Populate canvas from a ChainDefinition."""
        self._scene.clear()
        self._empty_text = None  # scene.clear() deleted the C++ object
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
        QTimer.singleShot(100, self._fit_view)

    def _fit_view(self) -> None:
        node_count = len(self._nodes)
        if node_count == 0:
            self.resetTransform()
            self.centerOn(0, 0)
        elif node_count == 1:
            self.resetTransform()
            node = next(iter(self._nodes.values()))
            self.centerOn(node.sceneBoundingRect().center())
        else:
            items_rect = self._scene.itemsBoundingRect()
            self.fitInView(items_rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)

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
        self._fit_view()
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
        scene_pos = self.mapToScene(event.pos())
        items = self._scene.items(scene_pos)
        if not items or all(isinstance(i, (QGraphicsTextItem,)) for i in items):
            menu = QMenu()
            add_draft_action = menu.addAction("Create Drone Here")
            action = menu.exec(event.globalPos())
            if action == add_draft_action:
                self._canvas_add_draft_node(scene_pos)
            return
        super().contextMenuEvent(event)

    def _canvas_add_draft_node(
        self,
        scene_pos: QPointF,
        draft_name: str = "Untitled Drone",
        draft_accepts: str = "",
        draft_produces: str = "",
    ) -> ChainNodeItem:
        node_id = f"draft-{uuid.uuid4().hex[:8]}"
        item = ChainNodeItem(
            node_id=node_id,
            drone=None,
            goal_template="",
            canvas=self,
            is_draft=True,
            draft_name=draft_name,
            draft_accepts=draft_accepts,
            draft_produces=draft_produces,
        )
        item.setPos(scene_pos - QPointF(NODE_WIDTH / 2, NODE_HEIGHT / 2))
        self._scene.addItem(item)
        self._nodes[node_id] = item
        self._update_empty_text()
        self._scene.clearSelection()
        item.setSelected(True)
        self.canvasChanged.emit()
        return item

    def create_draft_node(
        self,
        name: str = "Untitled Drone",
        accepts: str = "any",
        produces: str = "any",
    ) -> ChainNodeItem:
        center = self.mapToScene(self.viewport().rect().center())
        return self._canvas_add_draft_node(center, draft_name=name, draft_accepts=accepts, draft_produces=produces)

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
