from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QMenu,
)

from aura.gui.theme import ACCENT

if TYPE_CHECKING:
    from aura.gui.drones.chain_canvas import ChainCanvas
    from aura.gui.drones.chain_node_item import ChainNodeItem


PORT_RADIUS = 3
PORT_DIAMETER = PORT_RADIUS * 2


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
