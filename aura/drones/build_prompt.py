from __future__ import annotations

from aura.drones.build_spec import DroneBuildBrief


def _cap_requirements_lines(plan) -> list[str]:
    """Build instruction lines for capability requirements or skip."""
    lines: list[str] = []
    if plan.capability_requirements:
        lines.append(
            "2. For the capability_requirements above, call resolve_capability."
        )
        lines.append(
            "3. Merge resolved tools with the allowed_tools from the plan."
        )
        lines.append(
            "4. Populate capability_bindings and setup_steps from the resolution."
        )
    else:
        lines.append(
            "2. No external capabilities needed \u2014 skip resolve_capability."
        )
    return lines


def _generated_code_line(plan) -> str:
    if not plan.generated_code_allowed:
        return (
            "6. DO NOT create helper scripts, generated code, or dynamic tools."
        )
    return (
        "6. Generated code is allowed for this Drone \u2014 use it only "
        "for the specific new tool/integration."
    )


def build_drone_creation_prompt(brief: DroneBuildBrief) -> str:
    """Return a Planner-facing prompt to build a Drone from an approved brief.

    Uses the deterministic build compiler to produce a compiled build plan,
    then embeds the plan in the prompt so the Planner does not need to
    independently assess tool inventory or schema.
    """
    from aura.drones.build_compiler import compile_drone_build_plan
    from aura.drones.definition import default_tools_for_policy

    # Use the full harness tool surface as available tools
    available = frozenset(default_tools_for_policy("normal_diff_approval"))
    plan = compile_drone_build_plan(brief.build_brief, available)

    lines: list[str] = []
    lines.append(
        "The user has approved this Drone Build Brief. Build the Drone."
    )
    lines.append("")
    lines.append("## Build Brief")
    lines.append(brief.build_brief)
    lines.append("")
    lines.append("## Compiled Build Plan")
    lines.append(f"- allowed_tools: {list(plan.allowed_tools)}")
    if plan.capability_requirements:
        lines.append("- capability_requirements to resolve:")
        for cr in plan.capability_requirements:
            lines.append(f"  - {cr.capability}: {cr.purpose}")
    if plan.warnings:
        lines.append("- warnings:")
        for w in plan.warnings:
            lines.append(f"  - {w}")
    lines.append(f"- generated_code_allowed: {plan.generated_code_allowed}")
    lines.append("")
    lines.append("## Instructions")
    lines.append(
        "1. Use the allowed_tools listed above directly in the DroneDefinition."
    )
    lines.extend(_cap_requirements_lines(plan))
    lines.append(
        "5. Call save_drone_definition with the complete DroneDefinition."
    )
    lines.append(_generated_code_line(plan))
    lines.append("")
    lines.append(
        "Do NOT create scripts \u2014 just call save_drone_definition."
    )
    return "\n".join(lines)
