from __future__ import annotations

from aura.drones.definition import DroneDefinition


def compute_capability_badges(drone: DroneDefinition) -> list[str]:
    """Return short badge labels for a drone's capability state.

    Returns empty list when no capability bindings or first_run_test exist.
    """
    badges: list[str] = []
    bindings = drone.capability_bindings

    if not bindings and not drone.first_run_test:
        return []

    pending = False
    has_mcp = False
    has_generated = False

    for b in bindings:
        if b.setup_status != "ready":
            pending = True
        rk = b.route_kind.lower()
        if "mcp" in rk:
            has_mcp = True
        if rk == "generated_code":
            has_generated = True

    if pending:
        badges.append("Needs setup")
    if has_mcp:
        badges.append("Uses MCP")
    if has_generated:
        badges.append("Generated tool")

    if drone.first_run_test:
        badges.append("First-run test available")

    return badges
