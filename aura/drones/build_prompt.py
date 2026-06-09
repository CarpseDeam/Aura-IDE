"""Build a user-message prompt from an approved DroneBuildSpec."""

from __future__ import annotations

from aura.drones.build_spec import DroneBuildSpec


def build_drone_creation_prompt(spec: DroneBuildSpec) -> str:
    """Return a single user-message string for the Planner/Worker pipeline.

    The prompt tells the Planner to dispatch a Worker that creates a saved
    Drone JSON file under ``.aura/drones/``.
    """
    lines: list[str] = []
    lines.append(
        "The user has approved this Drone Build Spec. Please build it."
    )
    lines.append("")
    lines.append("## Approved Drone Build Spec")
    lines.append("")
    lines.append(f"- **Name**: {spec.name}")
    lines.append(f"- **Kind**: {spec.kind}")
    lines.append(f"- **Job**: {spec.job}")
    lines.append(f"- **Trigger**: {spec.trigger}")
    lines.append(f"- **Required Access**: {', '.join(spec.required_access) if spec.required_access else '(none)'}")
    lines.append(f"- **Write Policy**: {spec.write_policy}")
    lines.append(f"- **Action Policy**: {spec.action_policy}")
    lines.append(f"- **Capabilities Needed**: {', '.join(spec.capabilities_needed) if spec.capabilities_needed else '(none)'}")
    lines.append(f"- **Missing Capabilities**: {', '.join(spec.missing_capabilities) if spec.missing_capabilities else '(none)'}")
    lines.append(f"- **Instructions**: {spec.instructions}")
    lines.append(f"- **Output Contract**: {spec.output_contract}")
    lines.append(f"- **Success Criteria**: {', '.join(spec.success_criteria) if spec.success_criteria else '(none)'}")
    lines.append(f"- **Build Status**: {spec.build_status}")
    lines.append(f"- **Boundaries**: {', '.join(spec.boundaries) if spec.boundaries else '(none)'}")
    lines.append(f"- **Assumptions**: {', '.join(spec.assumptions) if spec.assumptions else '(none)'}")
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
        "a safe id from the spec name."
    )
    lines.append(
        "4. Use ``default_tools_for_policy(write_policy)`` from "
        "``aura.drones.definition`` for ``allowed_tools``."
    )
    lines.append(
        "5. Include all required ``DroneDefinition`` fields: ``id``, ``name``, "
        "``description``, ``instructions``, ``write_policy``, ``allowed_tools``, "
        "``output_contract``, ``budget`` (use default), ``scope`` (use default), "
        "``enabled=True``, ``created_by=\"drone_workshop\"``, "
        "``created_at`` (now ISO), ``updated_at`` (now ISO)."
    )
    lines.append(
        "6. Do NOT create a second Drone system. Do NOT open or depend on "
        "``DroneEditorDialog``."
    )
    lines.append(
        "7. Keep the saved Drone focused on the approved spec. "
        "Do not add external browser, Gmail, or scheduler capabilities."
    )
    lines.append("")
    lines.append("Dispatch a Worker to create the saved Drone JSON file.")
    return "\n".join(lines)
