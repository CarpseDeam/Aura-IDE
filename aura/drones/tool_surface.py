from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.conversation.tools.registry import ToolRegistry
from aura.drones.capabilities import CapabilityBinding  # noqa: F401
from aura.drones.definition import TERMINAL_TOOLS, WRITE_TOOLS, DroneDefinition, default_tools_for_policy


@dataclass(frozen=True)
class DroneToolSurface:
    registry: ToolRegistry
    allowed_tools: frozenset[str]
    tool_defs: tuple[dict[str, Any], ...]
    setup_notes: tuple[str, ...] = ()


def build_drone_tool_surface(workspace_root: Path, drone: DroneDefinition) -> DroneToolSurface:
    # 1. Create a full registry
    registry = ToolRegistry(
        workspace_root=workspace_root,
        read_only=False,
        mode="single",
    )

    # 2. Determine allowed tools — treat empty allowed_tools as policy defaults
    # for backward compatibility with older saved Drone JSON.
    allowed_set: set[str] = set(
        drone.allowed_tools or default_tools_for_policy(drone.write_policy)
    )
    has_explicit_tools = bool(drone.allowed_tools)

    # 3. Process capability bindings
    setup_notes: list[str] = list(drone.setup_steps or ())

    for binding in drone.capability_bindings:
        cap_label = f"[capability:{binding.capability}]"

        if binding.setup_status != "ready":
            if binding.setup_notes:
                setup_notes.append(f"{cap_label} setup pending: {binding.setup_notes}")
            else:
                setup_notes.append(f"{cap_label} setup pending")
            continue

        if binding.route_kind == "mcp":
            if not binding.command or not binding.command.strip():
                setup_notes.append(f"{cap_label} MCP command missing")
                continue
            try:
                registry.connect_mcp_server(binding.command)
            except Exception as exc:
                setup_notes.append(f"{cap_label} MCP connection failed: {exc}")
                continue
            if not has_explicit_tools and binding.tool_names:
                allowed_set.update(binding.tool_names)
        else:
            if not has_explicit_tools and binding.tool_names:
                allowed_set.update(binding.tool_names)

    # 4. Apply read-only stripping AFTER bindings so write tools from
    #    bindings also get stripped.
    if drone.write_policy == "read_only":
        allowed_set.difference_update(WRITE_TOOLS)
        allowed_set.difference_update(TERMINAL_TOOLS)

    # 5. Re-fetch tool defs (MCP connects may have added schemas)
    all_defs = registry.tool_defs()

    # 6. Filter tool defs to the allowed set
    tool_defs = tuple(
        t for t in all_defs
        if t.get("function", {}).get("name") in allowed_set
    )

    return DroneToolSurface(
        registry=registry,
        allowed_tools=frozenset(allowed_set),
        tool_defs=tool_defs,
        setup_notes=tuple(setup_notes),
    )
