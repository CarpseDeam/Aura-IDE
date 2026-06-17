from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QGraphicsItem

from aura.drones.definition import DroneDefinition
from aura.gui.drones.chain_node_item import ChainNodeItem, NODE_WIDTH, NODE_HEIGHT
from aura.gui.drones.mission_core_item import MissionCoreItem


logger = logging.getLogger(__name__)

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
        item.loopToggled.connect(self.loopToggled.emit)
        item.loopToggled.connect(self._line_renderer.update_loop_state)
        self._update_empty_text()
        self.canvasChanged.emit()

    def _handle_mission_core_drop(self, mission_item: MissionCoreItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            logger.warning("Drop failed: could not load drone with id '%s'", drone_id)
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
        mc = self._mission_core
        if mc is None:
            self._line_renderer.clear()
            return
        order = list(self._nodes.values())
        loop_enabled = mc.loop_enabled
        self._line_renderer.set_context(mc, order, loop_enabled)
        self._line_renderer.rebuild()
