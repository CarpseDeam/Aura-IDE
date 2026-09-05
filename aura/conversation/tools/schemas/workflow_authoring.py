"""Root-only native Workflow authoring schemas, independent of execution."""

from __future__ import annotations

from copy import deepcopy

from aura.conversation.tools.schemas.agent_teams import build_run_agent_team_tool_def


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


def build_workflow_authoring_tools(model_targets=(), *, read_only: bool = False) -> list[dict]:
    identity = {"type": "string", "description": "Exact saved Workflow id from inspection or a save result."}
    revision = {"type": "string", "description": "Exact revision returned by the latest inspection or save."}
    tools = [
        _tool(
            "inspect_workflow",
            "Inspect a saved Workflow's full editable structure, Agents and revision. "
            "Pass an empty workflow_id to discover Workflows, reusable Agents and model targets. "
            "Use this for workflow setup even when the Agents execution switch is off. "
            "Resolve ambiguous names before changing anything.",
            {
                "workflow_id": {
                    "type": "string",
                    "description": "Saved Workflow id, or empty string to list available choices.",
                }
            },
        )
    ]
    if read_only:
        return tools
    # Share the flat semantic vocabulary with automatic teams. Only the run
    # task and ephemeral labels belong to the automatic execution wrapper.
    shape = deepcopy(build_run_agent_team_tool_def(model_targets=model_targets)["function"]["parameters"]["properties"])
    shape.pop("task")
    shape["name"] = shape.pop("team_name")
    shape["name"]["description"] = "A clear reusable Workflow name."
    shape["description"] = shape.pop("team_description")
    shape["description"]["description"] = "When to use this Workflow and what it accomplishes."
    shape["new_agents"]["description"] = "Only new specialists needed; reuse existing Agent ids when appropriate."
    shape["new_agents"]["items"]["properties"]["instructions"]["description"] = (
        "Stable reusable specialist instructions. Individual run tasks are supplied later."
    )
    shape["occurrences"]["items"]["properties"]["assignment"]["description"] = (
        "Reusable responsibility for this placement, such as 'Review the requested change'. "
        "Keep this run's specific feature, filenames and inputs in the execution task."
    )
    shape["occurrences"]["items"]["properties"]["alias"]["description"] = (
        "On update preserve the exact inspected alias for every surviving occurrence. "
        "Use a new short alias only for a new placement."
    )
    tools.extend(
        [
            _tool(
                "create_workflow",
                "Create and save a reusable native Workflow when the user asks to set one up. "
                "Choose a name, inherit the current model by default, and save as personal. "
                "This does not execute Agents. Discuss ideas in prose until creation is requested. "
                "Inspect existing choices first when reusing Agents or a named Workflow.",
                shape,
            ),
            _tool(
                "update_workflow",
                "Save a revision of the exact inspected Workflow. Supply its complete desired shape, "
                "preserving unchanged occurrence aliases and existing Agent ids. Omitting an occurrence "
                "removes it from this Workflow. Shared Agent definitions and permissions are never modified; "
                "use occurrence assignments or explicitly introduce a new specialist for a workflow-specific variant. "
                "This does not run the Workflow. Inspect again after a stale-revision error.",
                {"workflow_id": identity, "revision": revision, **shape},
            ),
            _tool(
                "undo_workflow_edit",
                "Undo the latest edit to this exact saved Workflow in this session. "
                "The revision check prevents an old action from overwriting newer work.",
                {"workflow_id": identity, "revision": revision},
            ),
        ]
    )
    return tools
