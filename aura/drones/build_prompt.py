"""Build a user-message prompt from an approved DroneBuildBrief."""

from __future__ import annotations

from aura.drones.build_spec import DroneBuildBrief


def build_drone_creation_prompt(brief: DroneBuildBrief) -> str:
    """Return a single user-message string for the Planner/Worker pipeline.

    The prompt tells the Planner to dispatch a Worker that creates a saved
    Drone JSON file under ``.aura/drones/``.
    """
    lines: list[str] = []
    lines.append(
        "The user has approved this Drone Build Brief. "
        "Please build the Drone described below."
    )
    lines.append("")
    lines.append("## Approved Drone Build Brief")
    lines.append("")
    lines.append(brief.build_brief)
    lines.append("")
    lines.append("## Instructions")
    lines.append("")
    lines.append(
        "1. Read ``aura/drones/definition.py`` and ``aura/drones/store.py`` "
        "to understand the ``DroneDefinition`` schema and ``DroneStore`` API."
    )
    lines.append(
        "2. Create a JSON file under ``.aura/drones/`` that is compatible "
        "with ``DroneDefinition``."
    )
    lines.append(
        "3. Use ``DroneStore.next_id(workspace_root, name)`` to generate "
        "a safe id from the Drone name."
    )
    lines.append(
        "4. Use ``default_tools_for_policy(write_policy)`` from "
        "``aura.drones.definition`` for ``allowed_tools``."
    )
    lines.append(
        "5. Include all required ``DroneDefinition`` fields. "
        "Read the schema from ``aura/drones/definition.py``."
    )
    lines.append(
        "6. Do NOT create a second Drone system. Do NOT open or depend on "
        "``DroneEditorDialog``."
    )
    lines.append(
        "7. Include any access/setup/safety notes from the brief in "
        "the Drone's instructions."
    )
    lines.append(
        "8. Ask the user only for details or access that are truly needed "
        "later (e.g. API keys at runtime). Store no secrets in the Drone definition."
    )
    lines.append(
        "9. Keep the saved Drone focused on the approved brief. "
        "Do not add external browser, Gmail, or scheduler capabilities."
    )
    lines.append("")
    lines.append("Dispatch a Worker to create the saved Drone JSON file.")
    return "\n".join(lines)
