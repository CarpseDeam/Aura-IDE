from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QGraphicsItem

from aura.drones.definition import DroneDefinition
from aura.gui.drones.chain_node_item import ChainNodeItem
from aura.gui.drones.mission_core_item import MissionCoreItem
from aura.gui.drones.goal_planet_item import GoalPlanetItem


MISSION_CORE_WIDTH = 240
MISSION_CORE_HEIGHT = 120
ASSIGNMENT_HEIGHT = 24


def _populate_planet_from_drone(drone: DroneDefinition) -> tuple[str, str]:
    """Derive a deterministic (title, objective) pair from a DroneDefinition."""
    if drone.name:
        title = f"{drone.name} Target"
    else:
        desc_words = (drone.description or "").split()
        if desc_words:
            title = " ".join(desc_words[:5])
        else:
            title = "Mission Goal"

    if drone.description:
        objective = drone.description
    elif drone.output_contract and drone.output_contract.get("description"):
        objective = drone.output_contract["description"]
    elif drone.instructions:
        objective = drone.instructions
    elif drone.name:
        objective = f"Complete the mission for {drone.name}"
    else:
        objective = "Complete the assigned task"

    return title, objective


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

    def _canvas_add_goal_planet(self, scene_pos: QPointF) -> None:
        self._canvas_add_goal_planet_with_data(scene_pos)

    def _canvas_add_goal_planet_with_data(self, scene_pos: QPointF, title: str = "", objective: str = "") -> GoalPlanetItem:
        goal_id = f"goal-{uuid.uuid4().hex[:6]}"
        gp_item = GoalPlanetItem(
            node_id=f"goal-planet-{uuid.uuid4().hex[:4]}",
            canvas=self,
            goal_id=goal_id,
        )
        gp_item.setPos(scene_pos)
        if title:
            gp_item.title = title
        if objective:
            gp_item.objective = objective
        self._scene.addItem(gp_item)
        self._goal_planets[goal_id] = gp_item
        self._update_empty_text()
        self.canvasChanged.emit()
        return gp_item

    def _create_goal_planet_for_drone(self, drone: DroneDefinition) -> GoalPlanetItem:
        title, objective = _populate_planet_from_drone(drone)
        if self._mission_core is not None:
            mc_pos = self._mission_core.pos()
            base_x = mc_pos.x() + 200
            base_y = mc_pos.y() - 40
        else:
            base_x = 160
            base_y = 0
        scene_pos = QPointF(base_x, base_y + len(self._goal_planets) * 100)
        return self._canvas_add_goal_planet_with_data(scene_pos, title, objective)

    def _ensure_goal_for_drone(self, drone: DroneDefinition, preferred_goal_id: str = "") -> str:
        if preferred_goal_id and preferred_goal_id in self._goal_planets:
            return preferred_goal_id
        selected = [gp for gp in self._goal_planets.values() if gp.isSelected()]
        if len(selected) == 1:
            return selected[0].goal_id
        if len(self._goal_planets) == 1:
            return next(iter(self._goal_planets))
        return self._create_goal_planet_for_drone(drone).goal_id

    def _create_drone_assignment(self, drone: DroneDefinition, goal_id: str) -> ChainNodeItem:
        if self._mission_core is None:
            self._canvas_add_mission_core(QPointF(-160, 0))
        node_id = f"{drone.id}-{uuid.uuid4().hex[:4]}"
        item = ChainNodeItem(
            node_id=node_id,
            drone=drone,
            goal_template="",
            canvas=self,
            is_assignment=True,
            goal_id=goal_id,
        )
        mc = self._mission_core
        mc_pos = mc.pos()
        assignment_index = sum(1 for n in self._nodes.values() if n.is_assignment)
        x = mc_pos.x() + MISSION_CORE_WIDTH / 2 + 4
        y = mc_pos.y() - MISSION_CORE_HEIGHT / 2 + 6 + assignment_index * (ASSIGNMENT_HEIGHT + 4)
        item.setPos(x, y)
        self._scene.addItem(item)
        self._nodes[node_id] = item
        mc.add_assigned_drone(drone.id)
        self._scene.clearSelection()
        item.setSelected(True)
        self._update_empty_text()
        self.canvasChanged.emit()
        return item

    def _handle_mission_core_drop(self, mission_item: MissionCoreItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        goal_id = self._ensure_goal_for_drone(drone, "")
        self._create_drone_assignment(drone, goal_id)

    def _handle_goal_planet_drop(self, planet_item: GoalPlanetItem, drone_id: str) -> None:
        from aura.drones.store import DroneStore
        drone = DroneStore.load_drone(self._get_workspace_root(), drone_id)
        if drone is None:
            return
        self._create_drone_assignment(drone, planet_item.goal_id)
