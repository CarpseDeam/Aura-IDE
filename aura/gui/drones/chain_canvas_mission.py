from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QGraphicsItem

from aura.drones.definition import DroneDefinition
from aura.gui.drones.chain_canvas_items import PortItem, ChainEdgeItem
from aura.gui.drones.chain_node_item import ChainNodeItem, NODE_WIDTH, NODE_HEIGHT
from aura.gui.drones.mission_core_item import MissionCoreItem


MISSION_CORE_WIDTH = 240
MISSION_CORE_HEIGHT = 120


class ChainCanvasMissionMixin:
    """Mixin providing mission/goal orchestration methods for ChainCanvas."""

    def _get_workspace_root(self) -> Path:
        """Walk up parents to find ChainEditor's workspace_root."""
        p = self.parent()
        while p is not None:
            ws = getattr(p, "workspace_root", None)
            if ws:
                return ws
            p = p.parent()
        return Path.cwd()

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

    def _handle_mission_core_drop(self, mission_item: MissionCoreItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        self._append_drone_to_chain(drone)

    def _append_drone_to_chain(self, drone: DroneDefinition) -> ChainNodeItem:
        if self._mission_core is None:
            self._canvas_add_mission_core(QPointF(-160, 0))
        mc = self._mission_core
        node_id = f"{drone.id}-{uuid.uuid4().hex[:4]}"

        item = ChainNodeItem(
            node_id=node_id,
            drone=drone,
            goal_template="",
            canvas=self,
        )

        # Position to the right of tail node (or right of MC if first)
        mc_pos = mc.pos()
        if self._nodes:
            tail = list(self._nodes.values())[-1]
            tail_pos = tail.pos()
            x = tail_pos.x() + NODE_WIDTH + 80
        else:
            x = mc_pos.x() + MISSION_CORE_WIDTH / 2 + 80
        y = mc_pos.y() - NODE_HEIGHT / 2

        item.setPos(x, y)
        self._scene.addItem(item)
        self._nodes[node_id] = item
        mc.add_assigned_drone(drone.id)
        self._rewire_linear_ring()
        self._scene.clearSelection()
        item.setSelected(True)
        self._update_empty_text()
        self.canvasChanged.emit()
        return item

    def _rewire_linear_ring(self) -> None:
        """Remove all edges and rebuild a linear ring:
        MC.output -> n0.input -> n0.output -> n1.input ... -> nN.output -> MC.input
        """
        mc = self._mission_core
        if mc is None:
            return

        # Remove all existing edges
        for edge in list(self._edges):
            self._scene.removeItem(edge)
        self._edges.clear()

        order = list(self._nodes.values())  # insertion order = run order
        if not order:
            return

        from aura.gui.drones.chain_canvas_items import PortItem, ChainEdgeItem

        # Give MC output and input ports if it doesn't have them yet
        if not hasattr(mc, 'output_port') or mc.output_port is None:
            mc.output_port = PortItem(mc, is_input=False)
            mc.output_port.setPos(MISSION_CORE_WIDTH / 2, 0)
        if not hasattr(mc, 'input_port') or mc.input_port is None:
            mc.input_port = PortItem(mc, is_input=True)
            mc.input_port.setPos(-MISSION_CORE_WIDTH / 2, 0)

        # MC.output -> order[0].input
        if order[0].input_port:
            e = ChainEdgeItem(
                source_port=mc.output_port,
                target_port=order[0].input_port,
                canvas=self,
            )
            self._scene.addItem(e)
            self._edges.append(e)

        # order[i].output -> order[i+1].input
        for i in range(len(order) - 1):
            src = order[i].output_port
            tgt = order[i + 1].input_port
            if src and tgt:
                e = ChainEdgeItem(source_port=src, target_port=tgt, canvas=self)
                self._scene.addItem(e)
                self._edges.append(e)

        # order[-1].output -> MC.input (loop-back)
        if order[-1].output_port and mc.input_port:
            e = ChainEdgeItem(
                source_port=order[-1].output_port,
                target_port=mc.input_port,
                canvas=self,
            )
            self._scene.addItem(e)
            self._edges.append(e)
