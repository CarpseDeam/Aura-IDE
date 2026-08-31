"""One box on the workflow canvas, and the ports that start a line from it.

A node item draws an *occurrence*: the Task, the Aura Result, or one placement
of a reusable agent. It knows its node id and nothing about storage — what it
shows arrives as a :class:`NodeVisual` and what the user does to it leaves as
a signal.

The two ports are the whole reason the canvas needs no edge-type dropdown.
The port on the right starts the automatic next Step. The port under the box,
labelled ``+ Sub-agent``, starts the optional helper relationship. Which line
you get is decided by which port you pulled from, so the two kinds can never
be confused for one another after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from aura.agents.graph_models import ConnectionKind, WorkflowNodeKind
from aura.gui.theme import (
    ACCENT,
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

#: What a box says about a run, and the colour it says it in. The keys are
#: :class:`aura.agents.workflow_runner.WorkflowStepState` values; the canvas
#: is handed strings so nothing here has to import the runtime.
RUN_STATE_COLORS: dict[str, str] = {
    "running": ACCENT,
    "succeeded": SUCCESS,
    "failed": DANGER,
    "cancelled": WARN,
    "skipped": FG_MUTED,
}

RUN_STATE_LABELS: dict[str, str] = {
    "running": "Running",
    "succeeded": "Done",
    "failed": "Failed",
    "cancelled": "Stopped",
    "skipped": "Not run",
}

NODE_WIDTH = 196.0
NODE_HEIGHT = 68.0
NODE_RADIUS = 9.0
PORT_RADIUS = 6.0

#: How far under the box the sub-agent port sits, and how wide its label is
#: allowed to be. Both are geometry the connection items also read.
SUB_PORT_DROP = 14.0
_SUB_PORT_LABEL = "+ Sub-agent"


@dataclass(frozen=True)
class NodeVisual:
    """Everything a node item paints, resolved by somebody else.

    ``missing`` is the one state that must never be silently tidied away: an
    occurrence whose agent definition cannot be read stays exactly where it
    was put, saying so.
    """

    node_id: str
    kind: WorkflowNodeKind
    title: str
    subtitle: str = ""
    missing: bool = False
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def invalid(self) -> bool:
        return self.missing or bool(self.issues)


class WorkflowNodeItem(QGraphicsObject):
    """One draggable box, with the ports that begin each kind of line."""

    geometry_changed = Signal(str)  # node id — while it is being dragged
    move_committed = Signal(str, float, float)  # node id, x, y — when let go
    port_pressed = Signal(str, str, object)  # node id, kind value, scene pos
    port_dragged = Signal(str, str, object)
    port_released = Signal(str, str, object)

    def __init__(self, visual: NodeVisual, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self._visual = visual
        self._editable = True
        self._run_state = ""
        self._pulse = 1.0
        self._press_position = QPointF()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

        self._step_port = _PortItem(ConnectionKind.STEP, self)
        self._step_port.setPos(NODE_WIDTH, NODE_HEIGHT / 2)
        self._sub_port = _PortItem(ConnectionKind.SUB_AGENT, self, label=_SUB_PORT_LABEL)
        self._sub_port.setPos(NODE_WIDTH / 2, NODE_HEIGHT + SUB_PORT_DROP)
        self._apply_visual()

    # ---- what it is --------------------------------------------------------

    @property
    def node_id(self) -> str:
        return self._visual.node_id

    @property
    def visual(self) -> NodeVisual:
        return self._visual

    def set_visual(self, visual: NodeVisual) -> None:
        self._visual = visual
        self._apply_visual()
        self.update()

    @property
    def run_state(self) -> str:
        return self._run_state

    def set_run_state(self, state: str) -> None:
        """Show what this step is doing, without rebuilding the canvas."""
        state = str(state or "")
        if state == self._run_state:
            return
        self._run_state = state
        self.update()

    def set_pulse(self, value: float) -> None:
        """One frame of the shared breathing clock, while this step runs."""
        self._pulse = max(0.0, min(1.0, float(value)))
        self.update()

    def set_editable(self, editable: bool) -> None:
        """A running turn freezes the canvas: nothing moves, no line starts."""
        self._editable = bool(editable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, self._editable)
        self._apply_visual()

    def _apply_visual(self) -> None:
        kind = self._visual.kind
        # The Aura Result is where a workflow ends, so nothing starts from it.
        self._step_port.setVisible(
            self._editable and kind is not WorkflowNodeKind.AURA_RESULT
        )
        # Helpers hang off agents. The two fixed ends are not agents.
        self._sub_port.setVisible(self._editable and kind is WorkflowNodeKind.AGENT)

    # ---- where lines attach ------------------------------------------------

    def anchor(self, kind: ConnectionKind, *, outgoing: bool) -> QPointF:
        """The scene point a line of *kind* leaves from or arrives at."""
        if kind is ConnectionKind.STEP:
            local = (
                QPointF(NODE_WIDTH, NODE_HEIGHT / 2)
                if outgoing
                else QPointF(0.0, NODE_HEIGHT / 2)
            )
        else:
            local = (
                QPointF(NODE_WIDTH / 2, NODE_HEIGHT)
                if outgoing
                else QPointF(NODE_WIDTH / 2, 0.0)
            )
        return self.mapToScene(local)

    # ---- Qt painting -------------------------------------------------------

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt naming
        return QRectF(-2.0, -2.0, NODE_WIDTH + 4.0, NODE_HEIGHT + 4.0)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(0.0, 0.0, NODE_WIDTH, NODE_HEIGHT), NODE_RADIUS, NODE_RADIUS
        )
        return path

    def paint(self, painter, option, widget=None) -> None:  # noqa: ANN001
        del option, widget
        visual = self._visual
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        body = QRectF(0.0, 0.0, NODE_WIDTH, NODE_HEIGHT)

        run_color = RUN_STATE_COLORS.get(self._run_state)
        painter.setBrush(QBrush(QColor(_fill_for(visual.kind))))
        if run_color is not None:
            border = QColor(run_color)
            if self._run_state == "running":
                # Only the opacity breathes; the geometry never moves, so a
                # running box reads as alive without drifting on the canvas.
                border.setAlphaF(self._pulse)
            painter.setPen(QPen(border, 2.4))
        else:
            painter.setPen(QPen(QColor(_border_for(visual, self.isSelected())), 2.0
                                if self.isSelected() or visual.invalid else 1.0))
        painter.drawRoundedRect(body, NODE_RADIUS, NODE_RADIUS)

        accent = QColor(run_color) if run_color is not None else QColor(_accent_for(visual))
        if run_color is not None and self._run_state == "running":
            accent.setAlphaF(self._pulse)
        painter.setBrush(QBrush(accent))
        painter.setPen(Qt.PenStyle.NoPen)
        stripe = QPainterPath()
        stripe.addRoundedRect(QRectF(0.0, 0.0, 4.0, NODE_HEIGHT), 2.0, 2.0)
        painter.drawPath(stripe)

        title_font = QFont(painter.font())
        title_font.setPixelSize(12)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(QColor(FG)))
        width = int(NODE_WIDTH - 24.0)
        painter.drawText(
            QRectF(12.0, 8.0, width, 18.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(title_font).elidedText(
                visual.title, Qt.TextElideMode.ElideRight, width
            ),
        )

        detail_font = QFont(painter.font())
        detail_font.setPixelSize(10)
        detail_font.setBold(False)
        painter.setFont(detail_font)
        painter.setPen(QPen(QColor(DANGER if visual.missing else FG_DIM)))
        painter.drawText(
            QRectF(12.0, 28.0, width, 30.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            QFontMetrics(detail_font).elidedText(
                visual.subtitle, Qt.TextElideMode.ElideRight, width * 2
            ),
        )

        badge = RUN_STATE_LABELS.get(self._run_state) or (
            visual.kind.label if visual.kind.is_fixed else ""
        )
        if badge:
            painter.setPen(QPen(QColor(run_color or FG_MUTED)))
            painter.drawText(
                QRectF(NODE_WIDTH - 92.0, 8.0, 80.0, 16.0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                badge,
            )

    # ---- Qt interaction ----------------------------------------------------

    def itemChange(self, change, value):  # noqa: N802 - Qt naming
        if change is QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.geometry_changed.emit(self.node_id)
        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._press_position = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().mouseReleaseEvent(event)
        if self._editable and self.pos() != self._press_position:
            self.move_committed.emit(self.node_id, self.pos().x(), self.pos().y())


class _PortItem(QGraphicsObject):
    """A small circle that starts one kind of line when pulled from."""

    def __init__(
        self,
        kind: ConnectionKind,
        node: WorkflowNodeItem,
        *,
        label: str = "",
    ) -> None:
        super().__init__(node)
        self._kind = kind
        self._node = node
        self._label = label
        self._hovered = False
        self.setAcceptHoverEvents(True)
        self.setZValue(11)
        self.setToolTip(
            "Drag to the next step in this workflow."
            if kind is ConnectionKind.STEP
            else "Drag to an Agent that helps this Agent."
        )

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt naming
        if not self._label:
            span = PORT_RADIUS + 3.0
            return QRectF(-span, -span, span * 2, span * 2)
        return QRectF(-52.0, -PORT_RADIUS - 3.0, 104.0, PORT_RADIUS * 2 + 6.0)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        span = PORT_RADIUS + 3.0
        path.addEllipse(QRectF(-span, -span, span * 2, span * 2))
        return path

    def paint(self, painter, option, widget=None) -> None:  # noqa: ANN001
        del option, widget
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        color = QColor(ACCENT if self._hovered else BORDER_STRONG)
        painter.setBrush(QBrush(QColor(BG_RAISED)))
        painter.setPen(QPen(color, 1.6))
        painter.drawEllipse(QPointF(0.0, 0.0), PORT_RADIUS, PORT_RADIUS)
        if self._label:
            font = QFont(painter.font())
            font.setPixelSize(9)
            painter.setFont(font)
            painter.setPen(QPen(QColor(ACCENT if self._hovered else FG_MUTED)))
            painter.drawText(
                QRectF(PORT_RADIUS + 4.0, -8.0, 96.0, 16.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._label,
            )

    def hoverEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()
        self._node.port_pressed.emit(
            self._node.node_id, self._kind.value, event.scenePos()
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()
        self._node.port_dragged.emit(
            self._node.node_id, self._kind.value, event.scenePos()
        )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        event.accept()
        self._node.port_released.emit(
            self._node.node_id, self._kind.value, event.scenePos()
        )


def _fill_for(kind: WorkflowNodeKind) -> str:
    return BG_RAISED if kind.is_fixed else BG_ALT


def _accent_for(visual: NodeVisual) -> str:
    if visual.missing:
        return DANGER
    if visual.issues:
        return WARN
    if visual.kind is WorkflowNodeKind.TASK:
        return SUCCESS
    if visual.kind is WorkflowNodeKind.AURA_RESULT:
        return ACCENT
    return BORDER_STRONG


def _border_for(visual: NodeVisual, selected: bool) -> str:
    if selected:
        return ACCENT
    if visual.missing:
        return DANGER
    if visual.issues:
        return WARN
    return BORDER


__all__ = [
    "RUN_STATE_COLORS",
    "RUN_STATE_LABELS",
    "NODE_HEIGHT",
    "NODE_RADIUS",
    "NODE_WIDTH",
    "PORT_RADIUS",
    "SUB_PORT_DROP",
    "NodeVisual",
    "WorkflowNodeItem",
]
