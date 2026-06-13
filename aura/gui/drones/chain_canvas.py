from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

NODE_WIDTH = 252
NODE_HEIGHT = 76
PORT_RADIUS = 3
PORT_DIAMETER = PORT_RADIUS * 2
NODE_RADIUS = 12
MISSION_CORE_WIDTH = 240
MISSION_CORE_HEIGHT = 120
ASSIGNMENT_WIDTH = 60
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
        is_assignment: bool = False,
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
        self._is_assignment = is_assignment
        self._run_status = "idle"

        # Ports — assignments have no ports
        if not is_assignment:
            self.input_port = PortItem(self, is_input=True)
            self.output_port = PortItem(self, is_input=False)
            self._position_ports()
        else:
            self.input_port = None
            self.output_port = None

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
    def is_assignment(self) -> bool:
        return self._is_assignment

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
    def run_status(self) -> str:
        return self._run_status

    @run_status.setter
    def run_status(self, value: str) -> None:
        self._run_status = value
        self.update()

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
        if self._is_assignment:
            return QRectF(0, 0, ASSIGNMENT_WIDTH, ASSIGNMENT_HEIGHT)
        return QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()

        # --- Assignment compact token (60x24) ---
        if self._is_assignment:
            # Background
            painter.setBrush(QBrush(QColor("#1a1a24")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 6, 6)

            # Border
            border_color = QColor("#3a3a4a")
            if self.isSelected():
                border_color = _qt_color(ACCENT)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(border_color, 1))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

            # Status dot (left) — 3px radius at (8, 12)
            _run_color_map = {
                "idle": QColor("#6e7382"),
                "pending": QColor("#e0af68"),
                "running": QColor("#7dcfff"),
                "completed": QColor("#9ece6a"),
                "failed": QColor("#f7768e"),
            }
            if self._is_draft:
                dot_color = QColor("#9d7cd8")
            else:
                dot_color = _run_color_map.get(self._run_status, _run_color_map["idle"])
            painter.setBrush(QBrush(dot_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(8, 12), 3, 3)

            # Drone name — elided, at x=16, width=36
            if self._is_draft:
                name_text = self._draft_name or "Untitled Drone"
            elif self._drone:
                name_text = self._drone.name
            else:
                name_text = "Missing Drone"

            font_name = QFont()
            font_name.setPixelSize(10)
            painter.setFont(font_name)
            painter.setPen(QPen(QColor("#eaecef")))
            fm_name = QFontMetrics(font_name)
            name_avail = 36
            name_text = fm_name.elidedText(name_text, Qt.TextElideMode.ElideRight, name_avail)
            painter.drawText(QRectF(16, 6, name_avail, 12),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name_text)
            return

        # --- Normal card body: flat dark glass fill ---
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


class MissionCoreItem(QGraphicsObject):
    """A canvas card representing the mission command center."""

    missionCoreChanged = Signal()
    runRequested = Signal()

    def __init__(self, node_id: str, canvas: ChainCanvas):
        super().__init__()
        self._node_id = node_id
        self._canvas = canvas
        self._title = "Mothership"
        self._goal = ""
        self._assigned_drone_ids: list[str] = []
        self._cargo_count = 0
        self._output_status = "idle"
        self._run_btn_rect = QRectF()
        self._run_btn_hovered = False
        self._drag_hovered = False

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(900)
        self._pulse_timer.timeout.connect(self._on_pulse_tick)
        self._pulse_timer.start()
        self._pulse_phase = 0.0

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptDrops(True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value
        self.missionCoreChanged.emit()
        self.update()

    @property
    def goal(self) -> str:
        return self._goal

    @goal.setter
    def goal(self, value: str) -> None:
        self._goal = value
        self.missionCoreChanged.emit()
        self.update()

    @property
    def assigned_drone_ids(self) -> list[str]:
        return self._assigned_drone_ids

    @assigned_drone_ids.setter
    def assigned_drone_ids(self, value: list[str]) -> None:
        self._assigned_drone_ids = list(value)
        self.missionCoreChanged.emit()
        self.update()

    def add_assigned_drone(self, drone_id: str) -> None:
        self._assigned_drone_ids.append(drone_id)
        self.missionCoreChanged.emit()
        self.update()

    def remove_assigned_drone(self, drone_id: str) -> None:
        if drone_id in self._assigned_drone_ids:
            self._assigned_drone_ids.remove(drone_id)
        self.missionCoreChanged.emit()
        self.update()

    def boundingRect(self) -> QRectF:
        w = MISSION_CORE_WIDTH
        h = MISSION_CORE_HEIGHT
        return QRectF(-w / 2, -h / 2, w, h)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()
        w = MISSION_CORE_WIDTH
        h = MISSION_CORE_HEIGHT

        # Card body
        painter.setBrush(QBrush(QColor("#1a1a24")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 8, 8)

        # Glow border
        glow_color = QColor(_qt_color(ACCENT))
        if self._output_status == "running":
            pulse_val = (math.sin(self._pulse_phase) + 1) / 2
            glow_alpha = 20 + int(pulse_val * 30)
        else:
            glow_alpha = 25
        if self.isSelected():
            glow_alpha = 60
        if self._drag_hovered:
            glow_alpha = 80
        glow_color.setAlpha(glow_alpha)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(glow_color, 4))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 7, 7)

        # Border
        border_color = QColor("#3a3a4a")
        if self.isSelected():
            border_color = _qt_color(ACCENT)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border_color, 1.5))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 7, 7)

        # Drag hover highlight
        if self._drag_hovered:
            highlight_color = QColor(_qt_color(ACCENT))
            highlight_color.setAlpha(40)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(highlight_color, 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 5, 5)

        # Title
        font_title = QFont()
        font_title.setPixelSize(12)
        font_title.setBold(True)
        painter.setFont(font_title)
        painter.setPen(QPen(_qt_color(ACCENT)))
        painter.drawText(
            QRectF(-w / 2 + 12, -h / 2 + 10, w - 24, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._title,
        )

        # Header beacon diamond
        beacon_color = QColor(_qt_color(ACCENT))
        if self._output_status == "running":
            pulse_val = (math.sin(self._pulse_phase) + 1) / 2
            beacon_alpha = 180 + int(pulse_val * 40)
        else:
            beacon_alpha = 200
        beacon_color.setAlpha(int(beacon_alpha))
        painter.setBrush(QBrush(beacon_color))
        painter.setPen(Qt.PenStyle.NoPen)
        beacon_path = QPainterPath()
        beacon_path.moveTo(QPointF(w / 2 - 24, -h / 2 + 16))
        beacon_path.lineTo(QPointF(w / 2 - 21, -h / 2 + 19))
        beacon_path.lineTo(QPointF(w / 2 - 24, -h / 2 + 22))
        beacon_path.lineTo(QPointF(w / 2 - 27, -h / 2 + 19))
        beacon_path.closeSubpath()
        painter.drawPath(beacon_path)

        # Section labels: Launch Bay & Cargo Bay
        font_section = QFont()
        font_section.setPixelSize(9)
        painter.setFont(font_section)
        painter.setPen(QPen(QColor("#a8aebb")))
        drone_count = len(self._assigned_drone_ids)
        launch_bay_text = f"\u25c7  Launch Bay: {drone_count} drones"
        painter.drawText(
            QRectF(-w / 2 + 12, -h / 2 + 34, w - 24, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            launch_bay_text,
        )
        cargo_bay_text = f"\u25c6  Cargo Bay: {self._cargo_count} items"
        painter.drawText(
            QRectF(-w / 2 + 12, -h / 2 + 48, w - 24, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            cargo_bay_text,
        )

        # Metrics row
        font_metrics = QFont()
        font_metrics.setPixelSize(9)
        painter.setFont(font_metrics)

        _status_map = {
            "completed": ("\u2713 completed", _qt_color(ACCENT)),
            "running": ("\u25ce running", _qt_color(ACCENT)),
            "failed": ("\u2717 failed", _qt_color(DANGER)),
            "idle": ("\u25cb idle", _qt_color(FG_MUTED)),
        }
        status_text, status_color = _status_map.get(
            self._output_status, ("\u25cb idle", _qt_color(FG_MUTED))
        )
        metrics_text = f"\u2699 {drone_count}  \U0001f4e6 {self._cargo_count}  {status_text}"

        painter.setPen(QPen(status_color))
        metrics_y = h / 2 - 42
        painter.drawText(
            QRectF(-w / 2 + 12, metrics_y, w - 24, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics_text,
        )

        # "Run Mission" button (bottom-right)
        btn_w = 100
        btn_h = 22
        btn_x = w / 2 - 8 - btn_w
        btn_y = h / 2 - 8 - btn_h
        self._run_btn_rect = QRectF(btn_x, btn_y, btn_w, btn_h)

        btn_alpha = 150 if self._run_btn_hovered else 80
        btn_color = QColor(_qt_color(ACCENT))
        btn_color.setAlpha(btn_alpha)
        painter.setBrush(QBrush(btn_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self._run_btn_rect, 5, 5)

        font_btn = QFont()
        font_btn.setPixelSize(10)
        font_btn.setBold(True)
        painter.setFont(font_btn)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            self._run_btn_rect,
            Qt.AlignmentFlag.AlignCenter,
            "\u25b6 Run Mission",
        )

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            self._drag_hovered = True
            self.update()
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        event.ignore()
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
            self._drag_hovered = False
            self.update()
            drone_id = bytes(event.mimeData().data("application/x-aura-drone-id")).decode("utf-8")
            self._canvas._handle_mission_core_drop(self, drone_id)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self._drag_hovered = False
        self.update()
        super().dragLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._run_btn_rect.contains(event.pos()):
            self.runRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def hoverMoveEvent(self, event) -> None:
        hovered = self._run_btn_rect.contains(event.pos())
        if hovered != self._run_btn_hovered:
            self._run_btn_hovered = hovered
            self.update()
        super().hoverMoveEvent(event)

    def hoverEnterEvent(self, event) -> None:
        self._run_btn_hovered = False
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._run_btn_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def _on_pulse_tick(self) -> None:
        self._pulse_phase += 0.15
        self.update()

    def to_dict(self) -> dict:
        return {
            "title": self._title,
            "goal": self._goal,
            "position": [self.pos().x(), self.pos().y()],
            "assigned_drone_ids": list(self._assigned_drone_ids),
        }

    def from_dict(self, data: dict) -> None:
        self._title = data.get("title", "Mothership")
        self._goal = data.get("goal", "")
        pos = data.get("position")
        if pos and len(pos) == 2:
            self.setPos(pos[0], pos[1])
        self._assigned_drone_ids = list(data.get("assigned_drone_ids", []))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._canvas._on_node_moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._canvas._on_selection_changed()
        return super().itemChange(change, value)


PALETTES: dict[str, tuple[str, str, str]] = {
    "ice":    ("#7ec8e3", "#c8e6ff", "#5b9ecf"),
    "lava":   ("#ff6b4a", "#ff9966", "#8b2500"),
    "ocean":  ("#3b82f6", "#1e40af", "#0a1628"),
    "forest": ("#4ade80", "#166534", "#0a2e0a"),
    "desert": ("#f59e0b", "#d97706", "#78350f"),
    "void":   ("#7c3aed", "#2e1065", "#0f0a1a"),
    "toxic":  ("#a3e635", "#4d7c0f", "#1a2e05"),
    "moon":   ("#cbd5e1", "#64748b", "#1e293b"),
    "nebula": ("#c084fc", "#7e22ce", "#2a0a4a"),
}


class GoalPlanetItem(QGraphicsObject):
    """A small planet-like node representing the mission goal."""

    planetChanged = Signal()

    def __init__(self, node_id: str, canvas: ChainCanvas):
        super().__init__()
        self._node_id = node_id
        self._canvas = canvas
        self._goal = ""
        self._glow_phase = 0.0
        self._seed: int = 0
        self._style: str = "auto"
        self._planet_cache: QPixmap | None = None
        self._cache_key: tuple = ()

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(1200)
        self._pulse_timer.timeout.connect(self._on_tick)
        self._pulse_timer.start()

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptDrops(True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._ensure_seed()

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def goal(self) -> str:
        return self._goal

    @goal.setter
    def goal(self, value: str) -> None:
        self._goal = value
        self.planetChanged.emit()
        self.update()

    def _on_tick(self) -> None:
        self._glow_phase += 0.12
        self.update()

    def _ensure_seed(self) -> None:
        import random as _random
        if self._seed == 0:
            self._seed = _random.randint(1, 2**31 - 1)
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        self._planet_cache = None
        self._cache_key = ()
        self.update()

    def _palette_for_seed(self) -> tuple:
        return list(PALETTES.values())[self._seed % len(PALETTES)]

    def reroll_seed(self) -> None:
        import random as _random
        self._seed = _random.randint(1, 2**31 - 1)
        self._invalidate_cache()

    def boundingRect(self) -> QRectF:
        return QRectF(-44, -44, 88, 88)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        center = QPointF(0, 0)
        radius = 30

        # Cache check
        is_sel = int(self.isSelected())
        key = (self._seed, is_sel)
        if self._planet_cache is None or self._cache_key != key:
            self._planet_cache = self._render_planet(radius)
            self._cache_key = key

        # Draw cached planet body (origin at -radius, -radius since pixmap has padding)
        pm = self._planet_cache
        offset = QPointF(-pm.width() / 2, -pm.height() / 2)
        painter.drawPixmap(offset, pm)

        # Pulsing atmosphere glow (not cached, animates)
        atmos_str, _, _ = self._palette_for_seed()
        atmos_color = QColor(atmos_str)
        glow_alpha = int(12 + math.sin(self._glow_phase) * 6)
        atmos_color.setAlpha(max(0, min(255, glow_alpha)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(atmos_color, 4))
        painter.drawEllipse(center, radius + 4, radius + 4)

        # Selected ring
        if self.isSelected():
            sel_color = _qt_color(ACCENT)
            sel_color.setAlpha(160)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(sel_color, 2))
            painter.drawEllipse(center, radius + 2, radius + 2)

        # Goal text below
        font = QFont()
        font.setPixelSize(10)
        painter.setFont(font)
        text_rect = QRectF(-40, 38, 80, 40)
        if self._goal:
            elided = self._goal[:50]
            if len(self._goal) > 50:
                elided += "\u2026"
            painter.setPen(QPen(QColor("#eaecef")))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided)
        else:
            font.setItalic(True)
            painter.setFont(font)
            painter.setPen(QPen(_qt_color(FG_MUTED)))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, "Click to describe the goal.")

    def _render_planet(self, radius: int) -> QPixmap:
        import random as _random
        rng = _random.Random(self._seed)

        # Pixmap size includes padding for glow + rings
        pad = 16
        size = radius * 2 + pad * 2
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(size / 2, size / 2)

        atmos_str, light_str, dark_str = self._palette_for_seed()
        atmos = QColor(atmos_str)
        light = QColor(light_str)
        dark = QColor(dark_str)

        # Outer soft atmosphere
        g_atmos = QRadialGradient(center, radius + 6)
        a_outer = QColor(atmos)
        a_outer.setAlpha(40)
        a_inner = QColor(atmos)
        a_inner.setAlpha(10)
        g_atmos.setColorAt(0.0, a_inner)
        g_atmos.setColorAt(0.7, a_outer)
        g_atmos.setColorAt(1.0, QColor(atmos.red(), atmos.green(), atmos.blue(), 0))
        p.setBrush(QBrush(g_atmos))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, radius + 6, radius + 6)

        # Planet body
        g_body = QRadialGradient(center, radius)
        g_body.setColorAt(0.0, light)
        g_body.setColorAt(1.0, dark)
        p.setBrush(QBrush(g_body))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, radius, radius)

        # Clip to planet circle
        clip_path = QPainterPath()
        clip_path.addEllipse(center, radius, radius)
        p.setClipPath(clip_path)

        # Surface bands
        band_color = QColor(dark)
        band_color.setAlpha(80)
        p.setBrush(QBrush(band_color))
        p.setPen(Qt.PenStyle.NoPen)
        num_bands = rng.randint(1, 3)
        for _ in range(num_bands):
            band_y = center.y() + rng.uniform(-radius * 0.6, radius * 0.6)
            band_h = rng.uniform(2, 8)
            band_w = rng.uniform(radius * 0.7, radius * 1.4)
            band_x = center.x() - band_w / 2 + rng.uniform(-4, 4)
            p.drawRoundedRect(QRectF(band_x, band_y - band_h / 2, band_w, band_h), 3, 3)

        # Darker accent band
        accent = QColor(dark)
        accent.setAlpha(120)
        p.setBrush(QBrush(accent))
        accent_y = center.y() + rng.uniform(-radius * 0.3, radius * 0.3)
        accent_h = rng.uniform(2, 5)
        accent_w = rng.uniform(radius * 0.5, radius * 1.0)
        p.drawRoundedRect(QRectF(center.x() - accent_w / 2, accent_y - accent_h / 2, accent_w, accent_h), 2, 2)

        # Speckles
        speck_color = QColor(light)
        speck_color.setAlpha(100)
        p.setBrush(QBrush(speck_color))
        num_speckles = rng.randint(12, 25)
        for _ in range(num_speckles):
            sx = center.x() + rng.uniform(-radius * 0.85, radius * 0.85)
            sy = center.y() + rng.uniform(-radius * 0.85, radius * 0.85)
            sr = rng.uniform(0.6, 1.8)
            if (sx - center.x()) ** 2 + (sy - center.y()) ** 2 < (radius - 2) ** 2:
                p.drawEllipse(QPointF(sx, sy), sr, sr)

        # Craters
        crater_color = QColor(dark)
        crater_color.setAlpha(140)
        p.setBrush(QBrush(crater_color))
        num_craters = rng.randint(3, 8)
        for _ in range(num_craters):
            cx = center.x() + rng.uniform(-radius * 0.7, radius * 0.7)
            cy = center.y() + rng.uniform(-radius * 0.7, radius * 0.7)
            cr = rng.uniform(1.5, 3.5)
            if (cx - center.x()) ** 2 + (cy - center.y()) ** 2 < (radius - 3) ** 2:
                p.drawEllipse(QPointF(cx, cy), cr, cr)

        p.setClipPath(QPainterPath(), Qt.ClipOperation.NoClip)

        # Optional rings (seed % 10 < 3)
        if self._seed % 10 < 3:
            ring_color = QColor(atmos)
            ring_color.setAlpha(80)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(ring_color, 1.5))
            ring_radius = radius + rng.uniform(6, 12)
            tilt = rng.uniform(0.3, 0.6)
            p.drawEllipse(QRectF(
                center.x() - ring_radius,
                center.y() - ring_radius * tilt,
                ring_radius * 2,
                ring_radius * 2 * tilt,
            ))

        p.end()
        return pix

    def dragEnterEvent(self, event) -> None:
        event.ignore()
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        event.ignore()
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        event.ignore()
        super().dropEvent(event)

    def to_dict(self) -> dict:
        return {
            "goal": self._goal,
            "seed": self._seed,
            "style": self._style,
            "position": [self.pos().x(), self.pos().y()],
        }

    def from_dict(self, data: dict) -> None:
        self._goal = data.get("goal", "")
        self._seed = data.get("seed", 0)
        self._style = data.get("style", "auto")
        if "position" in data and len(data["position"]) == 2:
            self.setPos(data["position"][0], data["position"][1])
        self._ensure_seed()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._canvas._on_node_moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._invalidate_cache()
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
            for w, alpha in [(4, 8), (2, 18)]:
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
        main_width = 1.2 if not self.isSelected() else 2.0
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
        delete_action = menu.addAction("Delete Edge")
        action = menu.exec(event.screenPos())
        if action == delete_action:
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
    runMissionRequested = Signal()

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
        self._goal_planet: GoalPlanetItem | None = None
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

        if not self._nodes and self._mission_core is None and self._goal_planet is None:
            text = QGraphicsTextItem(
                "Right-click to add a Mission Core and Goal Planet."
            )
            font = QFont()
            font.setPixelSize(11)
            text.setFont(font)
            text.setDefaultTextColor(_qt_color(FG_MUTED))
            text.setPos(-120, -15)
            self._scene.addItem(text)
            self._empty_text = text


    def load_chain(self, chain: ChainDefinition, drone_lookup: dict[str, DroneDefinition], mission_core_data: dict | None = None, goal_planet_data: dict | None = None) -> None:
        """Populate canvas from a ChainDefinition."""
        self._scene.clear()
        self._empty_text = None  # scene.clear() deleted the C++ object
        self._nodes.clear()
        self._edges.clear()
        self._drawing_source_port = None
        self._rubber_band = None
        self._mission_core = None
        self._goal_planet = None

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

        # Goal planet
        self._goal_planet = None
        if goal_planet_data:
            gp_item = GoalPlanetItem(
                node_id=f"goal-planet-{uuid.uuid4().hex[:4]}",
                canvas=self,
            )
            gp_item.from_dict(goal_planet_data)
            self._scene.addItem(gp_item)
            self._goal_planet = gp_item

        self._update_empty_text()

        # Auto-create default mission core + goal planet for empty chains
        if self._mission_core is None and self._goal_planet is None and not self._nodes:
            self._canvas_add_mission_core(QPointF(-160, 0))
            self._canvas_add_goal_planet(QPointF(160, 0))

        # Auto-fit after a short delay
        QTimer.singleShot(100, self._fit_view)

    def _fit_view(self) -> None:
        node_count = len(self._nodes)
        if node_count == 0 and self._mission_core is None and self._goal_planet is None:
            self.resetTransform()
            self.centerOn(0, 0)
        elif node_count == 1 and self._mission_core is None and self._goal_planet is None:
            self.resetTransform()
            node = next(iter(self._nodes.values()))
            self.centerOn(node.sceneBoundingRect().center())
        elif node_count == 0 and self._mission_core is not None:
            self.resetTransform()
            self.centerOn(self._mission_core.sceneBoundingRect().center())
        elif node_count == 0 and self._mission_core is None and self._goal_planet is not None:
            self.resetTransform()
            self.centerOn(self._goal_planet.sceneBoundingRect().center())
        else:
            items_rect = self._scene.itemsBoundingRect()
            self.fitInView(items_rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)

        # Clamp minimum zoom so canvas doesn't zoom out too far
        current_scale = self.transform().m11()
        if current_scale < 0.35:
            factor = 0.35 / current_scale
            self.scale(factor, factor)

    def to_chain_nodes_and_edges(self) -> tuple[list[dict], list[dict], dict | None]:
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
                "is_assignment": item.is_assignment,
            })

        edges = []
        for edge in self._edges:
            edges.append({
                "from_node": edge.from_node_id,
                "to_node": edge.to_node_id,
            })

        mission_dict = self._mission_core.to_dict() if self._mission_core else None
        return nodes, edges, mission_dict

    @property
    def goal_planet_data(self) -> dict | None:
        return self._goal_planet.to_dict() if self._goal_planet else None

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
        # Ignore drone drops on open canvas (drops on MissionCore/GoalPlanet
        # are handled by those items' dropEvent).
        if event.mimeData().hasFormat("application/x-aura-drone-id"):
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
                self._goal_planet = None
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
            if self._goal_planet is not None:
                add_goal_action.setEnabled(False)
            action = menu.exec(event.globalPos())
            if action == add_mission_action:
                self._canvas_add_mission_core(scene_pos)
            elif action == add_goal_action:
                self._canvas_add_goal_planet(scene_pos)
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
        if self._goal_planet is not None:
            return
        gp_item = GoalPlanetItem(
            node_id=f"goal-planet-{uuid.uuid4().hex[:4]}",
            canvas=self,
        )
        gp_item.setPos(scene_pos)
        self._scene.addItem(gp_item)
        self._goal_planet = gp_item
        self._update_empty_text()
        self.canvasChanged.emit()

    def _handle_mission_core_drop(self, mission_item: MissionCoreItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        node_id = f"{drone_id}-{uuid.uuid4().hex[:4]}"
        item = ChainNodeItem(
            node_id=node_id,
            drone=drone,
            goal_template="",
            canvas=self,
            is_assignment=True,
        )
        # Stack assignments vertically to the right of the mission core
        mc_pos = mission_item.pos()
        assignment_index = sum(1 for n in self._nodes.values() if n.is_assignment)
        x = mc_pos.x() + MISSION_CORE_WIDTH / 2 + 4
        y = mc_pos.y() - MISSION_CORE_HEIGHT / 2 + 6 + assignment_index * (ASSIGNMENT_HEIGHT + 4)
        item.setPos(x, y)
        self._scene.addItem(item)
        self._nodes[node_id] = item
        mission_item.add_assigned_drone(drone_id)
        self._scene.clearSelection()
        item.setSelected(True)
        self._update_empty_text()
        self.canvasChanged.emit()

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
        """Draw assignment connection lines from Mothership to assignments."""
        _saved = False
        try:
            mc = self._mission_core
            if not _is_valid_item(mc):
                return
            if mc.scene() is None:
                return

            mc_rect = mc.sceneBoundingRect()
            source_pt = QPointF(mc_rect.right(), mc_rect.center().y())

            painter.save()
            _saved = True

            line_color = _qt_color(ACCENT)
            line_color.setAlpha(25)
            pen = QPen(line_color)
            pen.setWidthF(0.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            for node in list(self._nodes.values()):
                if not _is_valid_item(node):
                    continue
                if node.scene() is None:
                    continue
                if not node.is_assignment:
                    continue
                node_rect = node.sceneBoundingRect()
                target_pt = QPointF(node_rect.left(), node_rect.center().y())
                painter.drawLine(source_pt, target_pt)
        except Exception:
            logger.exception("drawForeground error — suppressed to protect Qt paint cycle")
        finally:
            if _saved:
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
