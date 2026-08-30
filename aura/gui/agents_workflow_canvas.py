"""The workflow canvas: a QGraphicsScene of boxes and curves, and its view.

The scene owns items, not documents. It is handed a
:class:`~aura.agents.graph_models.WorkflowGraph` plus the visuals resolved for
it, draws exactly that, and reports what the user did as intent — a node let
go somewhere, a port pulled to another node, an end reconnected, a selection,
a deletion, an agent dropped. Every one of those is a signal; none of them
edits a document, touches disk, or decides whether what was asked for is
allowed. :class:`aura.gui.main_window_agents_graphs.AgentsGraphController`
answers all of that and hands back the next drawing.

The view adds the things a canvas is expected to do: wheel zoom under the
cursor, middle-button pan, rubber-band selection, Delete, and undo/redo.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsScene, QGraphicsView

from aura.agents.graph_models import ConnectionKind, WorkflowGraph
from aura.agents.graph_validation import GraphValidation
from aura.gui.agents_library import AGENT_MIME
from aura.gui.agents_workflow_edge import (
    SOURCE_END,
    TARGET_END,
    WorkflowConnectionItem,
)
from aura.gui.agents_workflow_node import NodeVisual, WorkflowNodeItem
from aura.gui.theme import ACCENT, BG, BORDER, FG_MUTED

#: Zoom bounds, so a canvas can never be scrolled into a state nobody can
#: read or find their way back from.
MIN_SCALE = 0.35
MAX_SCALE = 2.6

_GRID_SPACING = 28.0


class WorkflowScene(QGraphicsScene):
    """Draws one workflow and reports what was done to it."""

    node_moved = Signal(str, float, float)  # node id, x, y
    connect_requested = Signal(str, str, str)  # source node, target node, kind
    connection_rerouted = Signal(str, object)  # connection id, Point | None
    connection_reconnected = Signal(str, str, str)  # connection id, end, node id
    selection_changed = Signal(str, str)  # "node" | "connection" | "", id
    delete_requested = Signal(object, object)  # node ids, connection ids
    agent_dropped = Signal(str, float, float, str)  # source key, x, y, connection id

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(QColor(BG)))
        self._nodes: dict[str, WorkflowNodeItem] = {}
        self._edges: dict[str, WorkflowConnectionItem] = {}
        self._editable = True
        self._placeholder = "No workflow open yet."
        self._link_source = ""
        self._link_kind: ConnectionKind | None = None
        self._link_preview: QGraphicsPathItem | None = None
        self.selectionChanged.connect(self._emit_selection)

    # ---- drawing one workflow ----------------------------------------------

    def render_graph(
        self,
        graph: WorkflowGraph | None,
        visuals: dict[str, NodeVisual],
        validation: GraphValidation | None = None,
    ) -> None:
        """Rebuild every item from *graph*, keeping the current selection."""
        selected_nodes, selected_edges = self.selected_ids()
        self._clear_link_preview()
        self.blockSignals(True)
        self.clear()
        self._nodes = {}
        self._edges = {}
        self.blockSignals(False)
        if graph is None:
            self.setSceneRect(QRectF(-400.0, -240.0, 800.0, 480.0))
            self.update()
            return

        for node in graph.nodes:
            visual = visuals.get(node.node_id)
            if visual is None:
                continue
            item = WorkflowNodeItem(visual)
            item.setPos(node.position.x, node.position.y)
            item.setToolTip("\n".join(visual.issues))
            item.set_editable(self._editable)
            item.geometry_changed.connect(self._on_node_geometry)
            item.move_committed.connect(self.node_moved)
            item.port_pressed.connect(self._on_port_pressed)
            item.port_dragged.connect(self._on_port_dragged)
            item.port_released.connect(self._on_port_released)
            self.addItem(item)
            self._nodes[node.node_id] = item

        for edge in sorted(graph.connections, key=lambda item: item.order):
            source = self._nodes.get(edge.source_id)
            target = self._nodes.get(edge.target_id)
            if source is None or target is None:
                continue
            issues = validation.for_connection(edge.connection_id) if validation else ()
            item = WorkflowConnectionItem(
                edge.connection_id,
                edge.kind,
                source,
                target,
                bend=edge.bend,
                invalid=bool(issues),
            )
            item.setToolTip("\n".join(issue.message for issue in issues))
            item.set_editable(self._editable)
            item.rerouted.connect(self.connection_rerouted)
            item.reconnect_released.connect(self._on_reconnect_released)
            self.addItem(item)
            self._edges[edge.connection_id] = item

        self._restore_selection(selected_nodes, selected_edges)
        self.setSceneRect(self.itemsBoundingRect().adjusted(-260.0, -200.0, 260.0, 200.0))
        self.update()

    def set_editable(self, editable: bool) -> None:
        self._editable = bool(editable)
        for node in self._nodes.values():
            node.set_editable(self._editable)
        for edge in self._edges.values():
            edge.set_editable(self._editable)

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    @property
    def editable(self) -> bool:
        return self._editable

    @property
    def node_items(self) -> dict[str, WorkflowNodeItem]:
        return self._nodes

    @property
    def edge_items(self) -> dict[str, WorkflowConnectionItem]:
        return self._edges

    # ---- selection ---------------------------------------------------------

    def selected_ids(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        nodes = tuple(
            item.node_id
            for item in self.selectedItems()
            if isinstance(item, WorkflowNodeItem)
        )
        edges = tuple(
            item.connection_id
            for item in self.selectedItems()
            if isinstance(item, WorkflowConnectionItem)
        )
        return nodes, edges

    def select_node(self, node_id: str) -> bool:
        item = self._nodes.get(node_id)
        if item is None:
            return False
        self.clearSelection()
        item.setSelected(True)
        return True

    def _restore_selection(
        self, nodes: tuple[str, ...], edges: tuple[str, ...]
    ) -> None:
        self.blockSignals(True)
        for node_id in nodes:
            item = self._nodes.get(node_id)
            if item is not None:
                item.setSelected(True)
        for edge_id in edges:
            edge = self._edges.get(edge_id)
            if edge is not None:
                edge.setSelected(True)
        self.blockSignals(False)
        self._emit_selection()

    def _emit_selection(self) -> None:
        nodes, edges = self.selected_ids()
        if len(nodes) == 1 and not edges:
            self.selection_changed.emit("node", nodes[0])
        elif len(edges) == 1 and not nodes:
            self.selection_changed.emit("connection", edges[0])
        else:
            self.selection_changed.emit("", "")

    def delete_selection(self) -> None:
        """Ask for everything selected to go. Fixed ends are never included."""
        if not self._editable:
            return
        nodes, edges = self.selected_ids()
        removable = tuple(
            node_id
            for node_id in nodes
            if not self._nodes[node_id].visual.kind.is_fixed
        )
        if removable or edges:
            self.delete_requested.emit(removable, edges)

    # ---- hit testing -------------------------------------------------------

    def node_at(self, position: QPointF) -> WorkflowNodeItem | None:
        for item in self.items(position):
            if isinstance(item, WorkflowNodeItem):
                return item
            parent = item.parentItem()
            if isinstance(parent, WorkflowNodeItem):
                return parent
        return None

    def step_connection_at(self, position: QPointF) -> WorkflowConnectionItem | None:
        """The solid line under *position*, which an agent can be dropped into."""
        for item in self.items(position):
            if (
                isinstance(item, WorkflowConnectionItem)
                and item.kind is ConnectionKind.STEP
            ):
                return item
        return None

    # ---- live geometry -----------------------------------------------------

    def _on_node_geometry(self, node_id: str) -> None:
        """Follow a node that is being dragged, curve by curve."""
        for edge in self._edges.values():
            if node_id in (edge.source_node_id, edge.target_node_id):
                edge.refresh()

    # ---- pulling a new line out of a port ----------------------------------

    def _on_port_pressed(self, node_id: str, kind_value: str, position) -> None:
        if not self._editable:
            return
        kind = ConnectionKind.parse(kind_value)
        if kind is None:
            return
        self._link_source = node_id
        self._link_kind = kind
        preview = QGraphicsPathItem()
        pen = QPen(QColor(ACCENT), 1.8)
        pen.setStyle(
            Qt.PenStyle.SolidLine
            if kind is ConnectionKind.STEP
            else Qt.PenStyle.DashLine
        )
        preview.setPen(pen)
        preview.setZValue(30)
        self.addItem(preview)
        self._link_preview = preview
        self._on_port_dragged(node_id, kind_value, position)

    def _on_port_dragged(self, node_id: str, kind_value: str, position) -> None:
        preview = self._link_preview
        source = self._nodes.get(node_id)
        kind = ConnectionKind.parse(kind_value)
        if preview is None or source is None or kind is None:
            return
        start = source.anchor(kind, outgoing=True)
        path = QPainterPath(start)
        reach = max(48.0, abs(position.x() - start.x()) * 0.45)
        if kind is ConnectionKind.STEP:
            path.cubicTo(
                start + QPointF(reach, 0.0), position - QPointF(reach, 0.0), position
            )
        else:
            drop = max(40.0, abs(position.y() - start.y()) * 0.5)
            path.cubicTo(
                start + QPointF(0.0, drop), position - QPointF(0.0, drop), position
            )
        preview.setPath(path)

    def _on_port_released(self, node_id: str, kind_value: str, position) -> None:
        self._clear_link_preview()
        kind = ConnectionKind.parse(kind_value)
        target = self.node_at(position)
        self._link_source = ""
        self._link_kind = None
        if kind is None or target is None or target.node_id == node_id:
            return
        self.connect_requested.emit(node_id, target.node_id, kind.value)

    def _clear_link_preview(self) -> None:
        preview = self._link_preview
        self._link_preview = None
        if preview is not None and preview.scene() is self:
            self.removeItem(preview)

    def _on_reconnect_released(self, connection_id: str, end: str, position) -> None:
        target = self.node_at(position)
        if target is None or end not in (SOURCE_END, TARGET_END):
            # Nothing under the cursor: the line snaps back to where it was.
            edge = self._edges.get(connection_id)
            if edge is not None:
                edge.refresh()
            return
        self.connection_reconnected.emit(connection_id, end, target.node_id)

    # ---- background --------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        painter.fillRect(rect, QBrush(QColor(BG)))
        painter.setPen(QPen(QColor(BORDER), 1.0, Qt.PenStyle.SolidLine))
        left = rect.left() - (rect.left() % _GRID_SPACING)
        top = rect.top() - (rect.top() % _GRID_SPACING)
        dots = [
            QPointF(x, y)
            for x in _steps(left, rect.right())
            for y in _steps(top, rect.bottom())
        ]
        if len(dots) <= 4000:
            painter.drawPoints(dots)
        if not self._nodes and self._placeholder:
            painter.setPen(QPen(QColor(FG_MUTED)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._placeholder)


def _steps(start: float, end: float) -> list[float]:
    values: list[float] = []
    current = start
    while current < end:
        values.append(current)
        current += _GRID_SPACING
    return values


class WorkflowView(QGraphicsView):
    """Pan, zoom, rubber-band selection, deletion, undo/redo, and drops."""

    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, scene: WorkflowScene, parent=None) -> None:  # noqa: ANN001
        super().__init__(scene, parent)
        self._scene = scene
        self._panning = False
        self._pan_origin = QPointF()
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(f"QGraphicsView {{ border: 1px solid {BORDER}; }}")

    # ---- zoom and pan ------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        step = 1.0015 ** event.angleDelta().y()
        scale = self.transform().m11() * step
        if scale < MIN_SCALE or scale > MAX_SCALE:
            return
        self.scale(step, step)

    def reset_zoom(self) -> None:
        self.resetTransform()

    def fit_to_content(self) -> None:
        bounds = self._scene.itemsBoundingRect()
        if bounds.isEmpty():
            return
        self.fitInView(bounds.adjusted(-40.0, -40.0, 40.0, 40.0), Qt.AspectRatioMode.KeepAspectRatio)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() is Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_origin = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._panning:
            delta = event.position() - self._pan_origin
            self._pan_origin = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._panning and event.button() is Qt.MouseButton.MiddleButton:
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ---- keys --------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo_requested.emit()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Redo):
            self.redo_requested.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._scene.delete_selection()
            event.accept()
            return
        if event.key() == Qt.Key.Key_0 and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.reset_zoom()
            event.accept()
            return
        if event.key() == Qt.Key.Key_F:
            self.fit_to_content()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- dropping an agent -------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._accepts(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._accepts(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._accepts(event):
            super().dropEvent(event)
            return
        source_key = bytes(event.mimeData().data(AGENT_MIME)).decode("utf-8")
        scene_pos = self.mapToScene(event.position().toPoint())
        connection = self._scene.step_connection_at(scene_pos)
        self._scene.agent_dropped.emit(
            source_key,
            scene_pos.x(),
            scene_pos.y(),
            connection.connection_id if connection is not None else "",
        )
        event.acceptProposedAction()

    def _accepts(self, event) -> bool:  # noqa: ANN001
        return bool(
            self._scene.editable and event.mimeData().hasFormat(AGENT_MIME)
        )


__all__ = ["MAX_SCALE", "MIN_SCALE", "WorkflowScene", "WorkflowView"]
