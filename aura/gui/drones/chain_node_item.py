from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
)

from aura.drones.definition import DroneDefinition
from aura.gui.drones.chain_canvas_items import PortItem
from aura.gui.theme import (
    ACCENT,
    DANGER,
    FG_MUTED,
)

if TYPE_CHECKING:
    from aura.gui.drones.chain_canvas import ChainCanvas


NODE_WIDTH = 252
NODE_HEIGHT = 76
NODE_RADIUS = 12
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
        goal_id: str = "",
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
        self._goal_id = goal_id
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
    def goal_id(self) -> str:
        return self._goal_id

    @goal_id.setter
    def goal_id(self, value: str) -> None:
        self._goal_id = value
        self.update()

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
