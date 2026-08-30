"""Tool catalog for Aura's production and read-only capability surfaces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aura.agents.local_state import AgentPermission
from aura.conversation.tools.capability_groups import GODOT, tool_names_for
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect
from aura.conversation.tools.schemas import (
    AGENT_CHANGE_SET_TOOL_DEFS,
    APPLY_PATCH_TOOL_DEF,
    GIT_TOOL_DEFS,
    LOAD_SKILLS_TOOL_DEF,
    READ_SKILL_RESOURCE_TOOL_DEF,
    READ_TOOL_DEFS,
    REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF,
    TASK_CHECKLIST_TOOL_DEF,
    TERMINAL_TOOL_DEF,
    build_delegate_agent_tool_def,
    build_run_workflow_tool_def,
    build_workflow_helper_tool_def,
)

# Godot remains implemented and registered, but is not part of the ordinary
# model-facing catalogs.
GODOT_TOOL_NAMES: frozenset[str] = tool_names_for({GODOT})

#: The single production read tool. Line windows via offset/limit already
#: cover the rest of what a bulk-read wrapper would add.
PRODUCTION_READ_TOOL_NAMES: frozenset[str] = frozenset({"read_file"})

#: The single production repository-search tool. Discovery and bulk
#: inspection are ordinary ``shell`` work (``rg --files``, ``Get-ChildItem``,
#: git) rather than a second dedicated tool.
PRODUCTION_SEARCH_TOOL_NAMES: frozenset[str] = frozenset({"grep_search"})

#: The compact observation surface Read-Only Mode offers in addition to the
#: production read/search tools. Unrestricted ``shell`` cannot be offered in a
#: mode whose whole promise is that nothing it offers can mutate.
READ_ONLY_EXTRA_TOOL_NAMES: frozenset[str] = frozenset({"glob"})

#: The production mutation tool — the one edit tool the model acts through.
#: Used by the effect-model tests to prove every mutation tool is explicitly
#: classified.
MUTATION_TOOL_NAMES: frozenset[str] = frozenset({
    "apply_patch",
    "edit_godot_scene",
    "edit_godot_editor",
    "edit_godot_asset_preview",
    "install_godot_editor_bridge",
})


def _tool_name(tool_def: dict[str, Any]) -> str:
    fn = tool_def.get("function")
    return str(fn.get("name", "")) if isinstance(fn, dict) else ""


#: The fixed base surface a delegated child agent runs with: the production
#: read and search tools, ``glob``, and read-only Git. A solid workflow Step
#: may add its explicitly frozen helper schema after this base; no other child
#: gains a tool because of live graph or registry state.
CHILD_AGENT_TOOL_NAMES: frozenset[str] = (
    PRODUCTION_READ_TOOL_NAMES | PRODUCTION_SEARCH_TOOL_NAMES | READ_ONLY_EXTRA_TOOL_NAMES
)


def child_agent_tool_defs(
    permission: AgentPermission = AgentPermission.READ_ONLY,
    *,
    workflow_helpers: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The frozen tool catalog handed to one delegated child invocation.

    Every child gets read, search, and read-only Git. The exact frozen grant
    may add ``apply_patch`` and then ``shell``. There are never Skills,
    checklist or memory tools, MCP/dynamic tools, lifecycle operations, or
    ``delegate_agent``. A solid workflow Step is the one narrow exception: when
    its caller supplies frozen dashed helper rows, this invocation alone gets a
    helper-specific ``delegate_agent`` schema containing only those occurrence
    node ids. Ordinary children and helpers supply no rows, so delegation stays
    exactly one level deep.

    Extensible surface is absent by construction rather than by filtering —
    this function is handed no schemas to include, so nothing a server or a
    workspace script registers can reach a child.  The returned list is what
    the request offers, and the tool round refuses any call whose name is not
    in it.
    """
    permission = AgentPermission(permission)
    tools = [
        tool for tool in READ_TOOL_DEFS if _tool_name(tool) in CHILD_AGENT_TOOL_NAMES
    ] + list(GIT_TOOL_DEFS)
    if permission.allows_edit:
        tools.append(dict(APPLY_PATCH_TOOL_DEF))
    if permission.allows_terminal:
        tools.append(dict(TERMINAL_TOOL_DEF))
    helper_rows = tuple(workflow_helpers or ())
    if helper_rows:
        tools.append(build_workflow_helper_tool_def(helper_rows))
    return tools


class ToolCatalog:
    """Builds the available tool schemas for current capability facts."""

    def build_tool_defs(
        self,
        *,
        read_only: bool,
        dynamic_schemas: list[dict[str, Any]] | None = None,
        mcp_schemas: list[dict[str, Any]] | None = None,
        plan_review: bool = False,
        skills_active: bool = False,
        agents: tuple[dict[str, str], ...] | None = None,
        workflow_helpers: tuple[dict[str, str], ...] | None = None,
        workflow: dict[str, Any] | None = None,
        agent_change_sets: bool = False,
    ) -> list[dict[str, Any]]:
        """Build tool definitions for the production catalog.

        A normal production turn presents exactly five built-in capabilities
        — ``read_file``, ``grep_search``, ``apply_patch``, ``shell``, and
        ``update_task_checklist`` — plus whichever optional capabilities are
        actually active for this turn. Every optional capability is resolved
        once per real user turn and held for that turn's whole duration, so
        the exposed catalog — and therefore the provider's cached request
        prefix — never moves between rounds.

        ``plan_review`` adds ``review_implementation_plan`` when Plan Review is required for this
        turn (never in read-only mode: nothing read-only can mutate, so there
        is nothing for a plan to gate). ``skills_active`` adds ``load_skills``
        and ``read_skill_resource`` only when this turn's frozen skill index
        actually has candidates.

        ``agents`` adds ``delegate_agent``, and only when this turn's frozen
        roster actually holds an agent. With no roster — the ordinary case —
        the tool is absent from both catalogs and the surface is exactly what
        single-agent Aura has always offered. The rows carry each eligible
        agent's id, display name, and short description; an agent's full
        instructions are the child's brief and never enter this catalog.

        ``workflow`` adds ``run_agent_workflow``, and only when this turn
        froze a complete plan for a workflow the user switched on in the
        toolbar. With the switch off there is no plan, so there is no tool and
        no sentence about workflows anywhere in the request: the catalog and
        the prompt weigh exactly what they weighed before the feature existed.
        The row carries the workflow's name, its description, and each step's
        agent name and assignment — the shape of the hand-off, never an
        agent's instructions.

        Reading an explicitly authorized external location is not a separate
        capability and never changes this catalog: ``read_file`` and
        ``grep_search`` accept such a path directly.

        Provider-hosted web search is likewise not in this catalog, in either
        mode. It is a server tool inside the selected provider's own request,
        owned by ``aura.providers.native_search``; it never becomes a client
        function and never reaches ToolRunner. That is why a Read Only turn
        can still be grounded on the web while this catalog stays exactly the
        existing local read/git surface — no ``apply_patch``, no ``shell``, no
        lifecycle or dynamic mutation tools, and no Aura ``web_search`` proxy.
        """
        if read_only:
            tools: list[dict[str, Any]] = [
                tool for tool in READ_TOOL_DEFS
                if _tool_name(tool)
                in (PRODUCTION_READ_TOOL_NAMES | PRODUCTION_SEARCH_TOOL_NAMES | READ_ONLY_EXTRA_TOOL_NAMES)
            ] + list(GIT_TOOL_DEFS)
        else:
            # Production: one continuous model owns the whole request. The
            # catalog is the minimum a normal implementation turn needs — one
            # obvious route for reading known content, one for repository
            # search, one for filesystem edits, one for commands, and one
            # simple visible TODO — and it is stable: the same set on every
            # active request of the turn.
            production_read_tools = [
                tool for tool in READ_TOOL_DEFS
                if _tool_name(tool) in (PRODUCTION_READ_TOOL_NAMES | PRODUCTION_SEARCH_TOOL_NAMES)
            ]
            tools = (
                production_read_tools
                + [dict(TASK_CHECKLIST_TOOL_DEF)]
                + [dict(APPLY_PATCH_TOOL_DEF)]
                + [dict(TERMINAL_TOOL_DEF)]
            )
            if plan_review:
                tools.append(dict(REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF))

        if skills_active:
            tools.append(dict(LOAD_SKILLS_TOOL_DEF))
            tools.append(dict(READ_SKILL_RESOURCE_TOOL_DEF))

        if workflow_helpers:
            tools.append(build_workflow_helper_tool_def(workflow_helpers))
        elif agents:
            tools.append(build_delegate_agent_tool_def(agents))

        if workflow:
            tools.append(build_run_workflow_tool_def(workflow))

        if agent_change_sets:
            if read_only:
                tools.extend(
                    tool
                    for tool in AGENT_CHANGE_SET_TOOL_DEFS
                    if _tool_name(tool) in {
                        "list_agent_change_sets", "inspect_agent_change_set"
                    }
                )
            else:
                tools.extend(dict(tool) for tool in AGENT_CHANGE_SET_TOOL_DEFS)

        if not read_only and dynamic_schemas:
            tools.extend(dynamic_schemas)

        if mcp_schemas:
            tools.extend(mcp_schemas)

        return tools

    def effect_for(self, name: str) -> ToolEffect:
        """Explicit effect classification for a built-in tool name.

        Built-ins must be classified: an unclassified built-in is a catalog
        bug and raises instead of silently defaulting to a guess.
        """
        try:
            return BUILTIN_TOOL_EFFECTS[name]
        except KeyError:
            raise KeyError(
                f"built-in tool '{name}' has no explicit effect classification; "
                "add it to BUILTIN_TOOL_EFFECTS in "
                "aura/conversation/tools/effects.py"
            ) from None
