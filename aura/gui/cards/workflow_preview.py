"""A small native, read-only projection of a Workflow's real topology."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from aura.agents.graph_dag import runnable_dag
from aura.agents.helper_topology import read_helper_topology
from aura.agents.workflow_document import WorkflowDocument
from aura.agents.workflow_layout import layout_workflow
from aura.gui.theme import BG_ALT, FG, FG_DIM, LABEL_AGENTS


class WorkflowPreview(QWidget):
    """Paint graph nodes and handoffs; editing belongs to the native canvas."""

    def __init__(self, document: WorkflowDocument, parent=None) -> None:
        super().__init__(parent)
        self.document = document
        self._graph = layout_workflow(document.graph)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Workflow diagram")
        self.setToolTip("Solid arrows: handoffs. Dashed arrows: optional Sub-agents.")

    def set_document(self, document: WorkflowDocument) -> None:
        self.document = document
        self._graph = layout_workflow(document.graph)
        self.updateGeometry()
        self.update()

    def geometry_for_width(self, width: int):
        span = max((node.position.x for node in self._graph.nodes), default=0) - min(
            (node.position.x for node in self._graph.nodes), default=0
        )
        vertical = width < 540 or span > 4 * 320
        rects = {}
        dag = runnable_dag(self._graph)
        helpers = {edge.target_id for edge in self._graph.connections if not edge.is_step}
        for node in self._graph.nodes:
            if vertical and dag is not None and node.node_id in helpers:
                continue
            x, y = node.position.x / 320, node.position.y / 180
            cx, cy = (y * 160, x * 78) if vertical else (x * 192, y * 75)
            rects[node.node_id] = QRectF(cx - 62, cy - 22, 124, 44)
        if vertical and dag is not None:
            topology = read_helper_topology(self._graph)
            for root_id in dag.node_ids:
                # Stack a helper tree in one side column instead of shrinking
                # the whole diagram for each extra level of nesting.
                root = rects[root_id]
                for helper in topology.preorder_for_root(root_id):
                    parent = rects[helper.immediate_parent_node_id]
                    rect = QRectF(root.center().x() + 82, parent.center().y() - 22, 124, 44)
                    while any(rect.adjusted(-6, -8, 6, 8).intersects(other) for other in rects.values()):
                        rect.translate(0, 78)
                    rects[helper.node_id] = rect
        bounds = QRectF()
        for rect in rects.values():
            bounds = bounds.united(rect)
        scale = min(1.0, max(1, width - 24) / max(1, bounds.width()))
        return rects, bounds, scale, vertical

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        _, bounds, scale, _ = self.geometry_for_width(width)
        return max(70, int(bounds.height() * scale + 28))

    def sizeHint(self) -> QSize:
        return QSize(600, self.heightForWidth(max(260, self.width())))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        height = self.heightForWidth(event.size().width())
        if self.minimumHeight() != height:
            self.setFixedHeight(height)

    def paintEvent(self, event) -> None:
        rects, bounds, scale, vertical = self.geometry_for_width(self.width())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate((self.width() - bounds.width() * scale) / 2, 14)
        painter.scale(scale, scale)
        painter.translate(-bounds.left(), -bounds.top())
        for edge in self._graph.connections:
            source, target = rects.get(edge.source_id), rects.get(edge.target_id)
            if source is None or target is None:
                continue
            edge_vertical = (
                vertical
                if edge.is_step
                else (abs(target.center().y() - source.center().y()) > abs(target.center().x() - source.center().x()))
            )
            start = (
                QPointF(source.center().x(), source.bottom())
                if edge_vertical
                else QPointF(source.right(), source.center().y())
            )
            end = (
                QPointF(target.center().x(), target.top())
                if edge_vertical
                else QPointF(target.left(), target.center().y())
            )
            color = QColor(LABEL_AGENTS if edge.is_step else FG_DIM)
            pen = QPen(color, 1.5)
            if not edge.is_step:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath(start)
            if edge_vertical:
                mid = (start.y() + end.y()) / 2
                path.cubicTo(QPointF(start.x(), mid), QPointF(end.x(), mid), end)
                tip = [end, end + QPointF(-3.5, -6), end + QPointF(3.5, -6)]
            else:
                mid = (start.x() + end.x()) / 2
                path.cubicTo(QPointF(mid, start.y()), QPointF(mid, end.y()), end)
                tip = [end, end + QPointF(-6, -3.5), end + QPointF(-6, 3.5)]
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF(tip))
        names = {entry.agent_id: entry.name for entry in self.document.agents}
        font = painter.font()
        font.setPixelSize(12)
        painter.setFont(font)
        for node in self._graph.nodes:
            rect = rects[node.node_id]
            painter.setPen(QPen(QColor(LABEL_AGENTS if node.is_agent else FG_DIM), 1))
            painter.setBrush(QColor(BG_ALT))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor(FG))
            label = names.get(node.agent_id, "Agent") if node.is_agent else node.kind.label
            label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, 112)
            painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignCenter, label)
        painter.end()
