"""One line between two boxes: its curve, its handles, and its routing.

Both kinds of connection are drawn as a cubic Bézier so the canvas reads as a
flow rather than a wiring diagram, and both follow their nodes live — the
curve is recomputed from the two anchor points every time either end moves,
so a drag never leaves a line behind.

A solid line is the automatic next Step. A dashed line labelled *Sub-agent*
is the optional helper relationship. Selecting either one exposes three
handles: a bend point in the middle, which is what "manual routing" means and
is saved with the workflow, and one at each end, which reconnects that end to
another node without destroying the line — so the routing a user shaped by
hand survives being pointed somewhere else.

The bend is stored as an offset from the curve's own resting midpoint rather
than as a scene coordinate, so it keeps its shape when the nodes it joins are
moved.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from aura.agents.graph_edits import SOURCE_END, TARGET_END
from aura.agents.graph_models import ConnectionKind, Point
from aura.gui.agents_workflow_node import WorkflowNodeItem
from aura.gui.theme import ACCENT, BG_RAISED, BORDER_STRONG, DANGER, FG_MUTED, WARN

#: How far the curve bulges away from a straight line before any manual
#: routing is applied. Enough to separate two lines that share a node.
_MIN_CONTROL_REACH = 62.0

#: The share of a bend offset that actually reaches the curve's midpoint, for
#: a cubic whose two control points both carry the offset. Dragging the handle
#: divides by this, so the handle lands exactly under the cursor.
_MID_SHARE = 0.75

_HANDLE_RADIUS = 5.0
_PICK_WIDTH = 16.0


class WorkflowConnectionItem(QGraphicsObject):
    """The curve between two node items, and everything done to it directly."""

    rerouted = Signal(str, object)  # connection id, Point | None
    reconnect_dragged = Signal(str, str, object)  # id, end, scene pos
    reconnect_released = Signal(str, str, object)

    def __init__(
        self,
        connection_id: str,
        kind: ConnectionKind,
        source: WorkflowNodeItem,
        target: WorkflowNodeItem,
        *,
        bend: Point | None = None,
        invalid: bool = False,
    ) -> None:
        super().__init__()
        self._connection_id = connection_id
        self._kind = kind
        self._source = source
        self._target = target
        self._bend = QPointF(bend.x, bend.y) if bend is not None else None
        self._invalid = bool(invalid)
        self._editable = True
        self._path = QPainterPath()
        self._loose_end = ""
        self._loose_pos = QPointF()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(1)

        self._bend_handle = _Handle(self, "bend")
        self._end_handles = {
            SOURCE_END: _Handle(self, SOURCE_END),
            TARGET_END: _Handle(self, TARGET_END),
        }
        self.refresh()
        self._sync_handles()

    # ---- what it is --------------------------------------------------------

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def kind(self) -> ConnectionKind:
        return self._kind

    @property
    def source_node_id(self) -> str:
        return self._source.node_id

    @property
    def target_node_id(self) -> str:
        return self._target.node_id

    def set_invalid(self, invalid: bool) -> None:
        self._invalid = bool(invalid)
        self.update()

    def set_editable(self, editable: bool) -> None:
        self._editable = bool(editable)
        self._sync_handles()

    def bend_point(self) -> Point | None:
        if self._bend is None:
            return None
        return Point(self._bend.x(), self._bend.y())

    def set_bend(self, bend: Point | None) -> None:
        self._bend = QPointF(bend.x, bend.y) if bend is not None else None
        self.refresh()

    # ---- geometry ----------------------------------------------------------

    def refresh(self) -> None:
        """Recompute the curve — called whenever either end has moved."""
        start, end = self._endpoints()
        first, second = _control_offsets(start, end, self._kind)
        offset = self._bend or QPointF(0.0, 0.0)
        control_one = start + first + offset
        control_two = end + second + offset

        path = QPainterPath(start)
        path.cubicTo(control_one, control_two, end)
        self.prepareGeometryChange()
        self._path = path
        self._control_two = control_two
        self._sync_handles()
        self.update()

    def _endpoints(self) -> tuple[QPointF, QPointF]:
        start = (
            self._loose_pos
            if self._loose_end == SOURCE_END
            else self._source.anchor(self._kind, outgoing=True)
        )
        end = (
            self._loose_pos
            if self._loose_end == TARGET_END
            else self._target.anchor(self._kind, outgoing=False)
        )
        return start, end

    def _resting_mid(self) -> QPointF:
        """Where the curve's midpoint would sit with no manual routing."""
        start, end = self._endpoints()
        first, second = _control_offsets(start, end, self._kind)
        return (start + (start + first) * 3.0 + (end + second) * 3.0 + end) / 8.0

    def mid_point(self) -> QPointF:
        offset = self._bend or QPointF(0.0, 0.0)
        return self._resting_mid() + offset * _MID_SHARE

    def bend_for_scene_pos(self, position: QPointF) -> Point:
        """The offset that would put the curve's midpoint under *position*."""
        delta = (position - self._resting_mid()) / _MID_SHARE
        return Point(delta.x(), delta.y())

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt naming
        return self._path.boundingRect().adjusted(-24.0, -24.0, 24.0, 24.0)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(_PICK_WIDTH)
        return stroker.createStroke(self._path)

    # ---- painting ----------------------------------------------------------

    def paint(self, painter, option, widget=None) -> None:  # noqa: ANN001
        del option, widget
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        color = QColor(self._color())
        pen = QPen(color, 2.4 if self.isSelected() else 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if self._kind is ConnectionKind.SUB_AGENT:
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setDashPattern([4.0, 4.0])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._path)

        _, end = self._endpoints()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawPolygon(_arrow_head(self._control_two, end))

        if self._kind is ConnectionKind.SUB_AGENT:
            mid = self.mid_point()
            font = QFont(painter.font())
            font.setPixelSize(9)
            painter.setFont(font)
            label = QRectF(mid.x() - 34.0, mid.y() - 17.0, 68.0, 14.0)
            painter.setBrush(QBrush(QColor(BG_RAISED)))
            painter.setPen(QPen(color, 1.0))
            painter.drawRoundedRect(label, 5.0, 5.0)
            painter.setPen(QPen(QColor(FG_MUTED)))
            painter.drawText(label, Qt.AlignmentFlag.AlignCenter, "Sub-agent")

    def _color(self) -> str:
        if self.isSelected():
            return ACCENT
        if self._invalid:
            return DANGER if self._kind is ConnectionKind.STEP else WARN
        return BORDER_STRONG

    # ---- handles -----------------------------------------------------------

    def itemChange(self, change, value):  # noqa: N802 - Qt naming
        if change is QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._sync_handles()
        return super().itemChange(change, value)

    def _sync_handles(self) -> None:
        visible = bool(self.isSelected() and self._editable)
        self._bend_handle.setVisible(visible)
        self._bend_handle.setPos(self.mid_point())
        start, end = self._endpoints()
        self._end_handles[SOURCE_END].setVisible(visible)
        self._end_handles[SOURCE_END].setPos(start)
        self._end_handles[TARGET_END].setVisible(visible)
        self._end_handles[TARGET_END].setPos(end)

    # ---- what the handles report -------------------------------------------

    def drag_handle(self, role: str, position: QPointF) -> None:
        if role == "bend":
            self.set_bend(self.bend_for_scene_pos(position))
            return
        self._loose_end = role
        self._loose_pos = position
        self.refresh()
        self.reconnect_dragged.emit(self._connection_id, role, position)

    def release_handle(self, role: str, position: QPointF) -> None:
        if role == "bend":
            self.rerouted.emit(self._connection_id, self.bend_point())
            return
        self._loose_end = ""
        self._loose_pos = QPointF()
        self.refresh()
        self.reconnect_released.emit(self._connection_id, role, position)


class _Handle(QGraphicsObject):
    """A grab point on a selected line: the bend, or one of its two ends."""

    def __init__(self, connection: WorkflowConnectionItem, role: str) -> None:
        super().__init__(connection)
        self._connection = connection
        self._role = role
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, False)
        self.setZValue(20)
        self.setVisible(False)
        self.setToolTip(
            "Drag to bend this connection."
            if role == "bend"
            else "Drag onto another node to reconnect this end."
        )

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt naming
        span = _HANDLE_RADIUS + 2.0
        return QRectF(-span, -span, span * 2, span * 2)

    def paint(self, painter, option, widget=None) -> None:  # noqa: ANN001
        del option, widget
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setBrush(QBrush(QColor(BG_RAISED)))
        painter.setPen(QPen(QColor(ACCENT), 1.6))
        if self._role == "bend":
            painter.drawEllipse(QPointF(0.0, 0.0), _HANDLE_RADIUS, _HANDLE_RADIUS)
        else:
            span = _HANDLE_RADIUS
            painter.drawRect(QRectF(-span, -span, span * 2, span * 2))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()
        self._connection.drag_handle(self._role, event.scenePos())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()
        self._connection.release_handle(self._role, event.scenePos())


def _control_offsets(
    start: QPointF, end: QPointF, kind: ConnectionKind
) -> tuple[QPointF, QPointF]:
    """How far each control point reaches out of its node, and in which axis."""
    if kind is ConnectionKind.STEP:
        reach = max(_MIN_CONTROL_REACH, abs(end.x() - start.x()) * 0.45)
        return QPointF(reach, 0.0), QPointF(-reach, 0.0)
    reach = max(_MIN_CONTROL_REACH * 0.8, abs(end.y() - start.y()) * 0.5)
    return QPointF(0.0, reach), QPointF(0.0, -reach)


def _arrow_head(control: QPointF, tip: QPointF) -> QPolygonF:
    """A small triangle at *tip*, aimed along the curve's final tangent."""
    dx = tip.x() - control.x()
    dy = tip.y() - control.y()
    angle = math.atan2(dy, dx) if (dx or dy) else 0.0
    size = 9.0
    spread = math.radians(24.0)
    return QPolygonF(
        [
            tip,
            QPointF(
                tip.x() - size * math.cos(angle - spread),
                tip.y() - size * math.sin(angle - spread),
            ),
            QPointF(
                tip.x() - size * math.cos(angle + spread),
                tip.y() - size * math.sin(angle + spread),
            ),
        ]
    )


__all__ = ["SOURCE_END", "TARGET_END", "WorkflowConnectionItem"]
