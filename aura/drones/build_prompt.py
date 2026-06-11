"""Build a user-message prompt from an approved DroneBuildBrief."""

from __future__ import annotations

from aura.drones.build_spec import DroneBuildBrief


def build_drone_creation_prompt(brief: DroneBuildBrief) -> str:
    """Return a single user-message string for the Planner/Worker pipeline.

    The prompt tells the Planner to dispatch a Worker that creates a saved
    Drone via the DroneStore API.
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
        "2. Extract the capabilities the Drone needs from the approved brief. "
        "Each capability should be a short label like \"read inbox\" or "
        "\"query database\"."
    )
    lines.append(
        "3. Call ``resolve_capability`` for each needed capability to discover "
        "which routes are available. Aura resolves capabilities through "
        "candidate providers — pick the simplest viable route for each capability."
    )
    lines.append(
        "4. Call ``save_drone_definition`` (available as a Worker tool) to persist the Drone. "
        "Pass all required DroneDefinition fields. "
        "If you omit ``id``, the tool will auto-generate a safe slug from the name. "
        "Do NOT call ``DroneStore.save_drone()`` directly — use the tool. "
        "Do NOT manually write ``.aura/drones/*.json`` files."
    )
    lines.append(
        "5. Populate the capability fields from the resolution:"
    )
    lines.append(
        "   - ``capability_requirements`` — the requirements extracted from the brief."
    )
    lines.append(
        "   - ``capability_bindings`` — the selected bindings from ``resolve_capability``."
    )
    lines.append(
        "   - ``setup_steps`` — any setup steps needed before the Drone can run."
    )
    lines.append(
        "   - ``first_run_test`` — a quick smoke test to verify the Drone works."
    )
    lines.append(
        "6. Set ``allowed_tools`` to the resolved flat tool names from the "
        "``resolve_capability`` result. Do not hardcode a fixed tool list — "
        "use the tools Aura resolved."
    )
    lines.append(
        "7. Include all other required ``DroneDefinition`` fields. "
        "Read the schema from ``aura/drones/definition.py``."
    )
    lines.append(
        "8. Do NOT create a second Drone system. Do NOT open or depend on "
        "``DroneEditorDialog``."
    )
    lines.append(
        "9. DO NOT create Python scripts (e.g., ``scripts/create_*.py``, ``scripts/verify_*.py``), "
        "helper files, verifier scripts, scratch files, or any repo artifacts for the Drone build. "
        "The only acceptable output is calling ``save_drone_definition``."
    )
    lines.append(
        "10. Include any access, setup, safety, and harness notes from the brief in "
        "the Drone's instructions. If runtime access or a connector is needed, "
        "describe that requirement clearly."
    )
    lines.append(
        "11. Store no secrets in the Drone definition. Ask the user only for "
        "details or access that are truly needed later (e.g. API keys at runtime)."
    )
    lines.append("")
    lines.append("The final expected result is a saved Drone visible in Drone Bay.")
    lines.append("Do not create scripts — just call ``save_drone_definition``.")
    lines.append("")
    lines.append("Dispatch a Worker to create the saved Drone.")
    return "\n".join(lines)
