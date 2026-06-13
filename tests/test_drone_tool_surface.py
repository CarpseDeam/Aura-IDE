from __future__ import annotations

import pytest

from aura.drones.definition import DroneDefinition
from aura.drones.tool_surface import build_drone_tool_surface


def test_drone_tool_surface_is_removed(tmp_path) -> None:
    drone = DroneDefinition(
        id="folder-drone",
        name="Folder Drone",
        description="Runs from its folder.",
        instructions="Run the entrypoint.",
        write_policy="read_only",
        output_contract="Return cargo.",
        runtime="python",
        entrypoint="main:run",
        smoke="smoke:run",
    )

    with pytest.raises(RuntimeError, match="folder entrypoint"):
        build_drone_tool_surface(tmp_path, drone)
