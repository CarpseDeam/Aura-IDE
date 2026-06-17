from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
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


NODE_WIDTH = 180
NODE_HEIGHT = 40
NODE_RADIUS = 12

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
        self._run_status = "idle"

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

        # Shadow glow (updated in _update_shadow)
        self._shadow = QGraphicsDropShadowEffect()
        self._shadow.setBlurRadius(22)
        self._shadow.setOffset(0)
        self.setGraphicsEffect(self._shadow)
        self._update_shadow()
        self.setZValue(1)

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

    def _update_shadow(self) -> None:
        cap_color = self.border_color
        alpha = 0.30 if self._hovered else 0.18
        c = QColor(cap_color)
        c.setAlphaF(alpha)
        self._shadow.setColor(c)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()

        # --- Normal card: glass card with capability dot + name ---
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Glass body fill — translucent dark
        painter.setBrush(QBrush(QColor(22, 24, 32, 230)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, NODE_RADIUS, NODE_RADIUS)

        # Top inner sheen — subtle vertical gradient overlay
        sheen_rect = QRectF(rect)
        sheen = QLinearGradient(0, 0, 0, rect.height() * 0.38)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 12))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, NODE_RADIUS, NODE_RADIUS)
        painter.setClipPath(clip_path)
        painter.fillRect(sheen_rect, sheen)
        painter.setClipping(False)

        # Border — hairline
        border_color = self.border_color if not self.isSelected() else _qt_color(ACCENT)
        border_alpha = 90 if not self.isSelected() else 170
        bc = QColor(border_color)
        bc.setAlpha(border_alpha)
        border_style = Qt.PenStyle.DashLine if self._is_draft else Qt.PenStyle.SolidLine
        pen_w = 2 if self.isSelected() else 1
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(bc, pen_w, border_style))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), NODE_RADIUS - 1, NODE_RADIUS - 1)

        # Capability dot (left) — 9px diameter (radius 4.5)
        dot_center = QPointF(20, NODE_HEIGHT / 2)
        dot_radius = 4.5
        cap_color = self.border_color

        # Soft glow disc behind dot (3x radius, ~25% alpha)
        glow_c = QColor(cap_color)
        glow_c.setAlphaF(0.25)
        painter.setBrush(QBrush(glow_c))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(dot_center, dot_radius * 3, dot_radius * 3)

        # Dot fill
        painter.setBrush(QBrush(cap_color))
        painter.drawEllipse(dot_center, dot_radius, dot_radius)

        # Name — 14px, weight ~650, #eaecef, vertically centered next to dot
        if self._is_draft:
            name = self._draft_name or "Untitled Drone"
        elif self._drone:
            name = self._drone.name
            if self._missing:
                name += " (missing)"
        else:
            name = "Missing Drone"

        font_name = QFont()
        font_name.setPixelSize(14)
        font_name.setWeight(65)  # ~ demi-bold
        painter.setFont(font_name)
        fm = QFontMetrics(font_name)
        name_x = 34
        name_avail = NODE_WIDTH - name_x - 14
        name_elided = fm.elidedText(name, Qt.TextElideMode.ElideRight, name_avail)
        painter.setPen(QPen(QColor("#eaecef")))
        painter.drawText(
            QRectF(name_x, 0, name_avail, NODE_HEIGHT),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name_elided,
        )

    def hoverEnterEvent(self, event) -> None:
        self._hovered = True
        self._update_shadow()
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hovered = False
        self._update_shadow()
        self.update()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._canvas._on_node_moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._canvas._on_selection_changed()
        return super().itemChange(change, value)
