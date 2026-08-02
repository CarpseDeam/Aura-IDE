"""Tool catalog - builds the list of tool schemas for the current mode/read-only state."""

from __future__ import annotations

import copy
from typing import Any

from aura.conversation.tools._drone_schemas import (
    CHECK_DRONE_RUN_TOOL_DEF,
    DECLARE_UI_CONTRACT_TOOL_DEF,
    LAUNCH_READ_ONLY_DRONE_TOOL_DEF,
    REGISTER_DRONE_FOLDER_TOOL_DEF,
    RUN_READ_ONLY_DRONE_TOOL_DEF,
)
from aura.conversation.tools._schemas import (
    DIAGNOSTIC_TOOL_DEF,
    DISPATCH_TOOL_DEF,
    GIT_TOOL_DEFS,
    READ_TOOL_DEFS,
    REPORT_BLOCKER_TOOL_DEF,
    RUN_AND_WATCH_TOOL_DEF,
    SUMMON_DRONE_TOOL_DEF,
    TERMINAL_TOOL_DEF,
    WEB_SEARCH_TOOL_DEF,
    WORKER_TODO_TOOL_DEF,
    WORKSPACE_SNAPSHOT_TOOL_DEF,
    WRITE_TOOL_DEFS,
)
from aura.conversation.tools._types import RegistryMode
from aura.conversation.tools.capability_groups import BULK_READ, CODE_INTEL, tool_names_for
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect

# Read and search tools the production single-agent catalog no longer offers.
#
# Every one of these is reachable through a tool that remains: line windows via
# ``read_file``'s offset/limit, multi-file reads via several ``read_file`` calls
# in one round, directory listing via ``glob``, and symbol/structure lookup via
# ``grep_search``. Presenting all of them made the model choose an approach
# before it could choose an action.
#
# The handlers stay registered, so a replayed historical tool call still runs,
# and Planner/Worker mode keep their existing sets.
SINGLE_SUPERSEDED_READ_TOOL_NAMES: frozenset[str] = tool_names_for(
    {BULK_READ, CODE_INTEL}
)

PLANNER_TOOL_NAMES = {
    "read_file",
    "read_files",
    "read_file_range",
    "read_task_context",
    "read_file_outline",
    "list_directory",
    "glob",
    "grep_search",
    "find_usages",
    "search_codebase",
    "code_intel_outline",
    "code_intel_references",
    "code_intel_dependents",
    "code_intel_audit",
    "inspect_godot_assets",
    "inspect_godot_asset_preview",
    "capture_godot_asset_preview",
    "inspect_godot_editor",
    "inspect_godot_api",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_log_file",
}

NORMAL_WORKER_WRITE_TOOL_NAMES = {
    "write_file",
    "patch_file",
    "delete_file",
    "edit_godot_scene",
    "edit_godot_editor",
    "edit_godot_asset_preview",
    "install_godot_editor_bridge",
}

#: The production mutation tools — the only tools that can carry out an
#: implementation decision. Same set the normal catalogs already register; the
#: focused action request narrows to these rather than introducing replacements.
MUTATION_TOOL_NAMES: frozenset[str] = frozenset(NORMAL_WORKER_WRITE_TOOL_NAMES)


def _tool_name(tool_def: dict[str, Any]) -> str:
    fn = tool_def.get("function")
    return str(fn.get("name", "")) if isinstance(fn, dict) else ""


def _planner_safe_tool_def(tool_def: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(tool_def)
    fn = copied.get("function")
    if not isinstance(fn, dict):
        return copied
    description = str(fn.get("description") or "")
    description = description.replace(" before answering or editing", " before answering or dispatching")
    description = description.replace("before editing", "before dispatching")
    description = description.replace("after editing", "after Worker edits")
    if "Planner must not edit files" not in description:
        description = (
            description.rstrip()
            + " Planner must not edit files; use gathered context only to answer "
            "or call dispatch_to_worker."
        )
    fn["description"] = description
    return copied


class ToolCatalog:
    """Builds the available tool schemas for the current mode/read-only state."""

    def build_tool_defs(
        self,
        *,
        mode: RegistryMode,
        read_only: bool,
        dynamic_schemas: list[dict[str, Any]] | None = None,
        mcp_schemas: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build tool definitions for the given mode and state."""
        if read_only:
            tools: list[dict[str, Any]] = list(READ_TOOL_DEFS) + list(GIT_TOOL_DEFS)
        elif mode == "planner":
            planner_read_tools = [
                _planner_safe_tool_def(tool)
                for tool in READ_TOOL_DEFS
                if _tool_name(tool) in PLANNER_TOOL_NAMES
            ]
            planner_git_tools = [
                _planner_safe_tool_def(tool)
                for tool in GIT_TOOL_DEFS
                if _tool_name(tool) in PLANNER_TOOL_NAMES
            ]
            tools = (
                planner_read_tools
                + planner_git_tools
                + [dict(DISPATCH_TOOL_DEF)]
                + [dict(SUMMON_DRONE_TOOL_DEF)]
                + [dict(LAUNCH_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(RUN_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(CHECK_DRONE_RUN_TOOL_DEF)]
                + [dict(DECLARE_UI_CONTRACT_TOOL_DEF)]
                + [dict(DIAGNOSTIC_TOOL_DEF)]
                + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]
                + [dict(WEB_SEARCH_TOOL_DEF)]
            )
        elif mode == "worker":
            worker_write_tools = [
                tool for tool in WRITE_TOOL_DEFS
                if _tool_name(tool) in NORMAL_WORKER_WRITE_TOOL_NAMES
            ]
            tools = (
                list(READ_TOOL_DEFS)
                + [dict(WORKER_TODO_TOOL_DEF)]
                + worker_write_tools
                + [dict(TERMINAL_TOOL_DEF)]
                + [dict(RUN_AND_WATCH_TOOL_DEF)]
                + list(GIT_TOOL_DEFS)
                + [dict(LAUNCH_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(RUN_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(CHECK_DRONE_RUN_TOOL_DEF)]
                + [dict(REGISTER_DRONE_FOLDER_TOOL_DEF)]
            )
        else:
            # Production single-agent mode: one continuous model owns
            # inspection → live TODO → edits → validation → repair.
            single_read_tools = [
                tool for tool in READ_TOOL_DEFS
                if _tool_name(tool) not in SINGLE_SUPERSEDED_READ_TOOL_NAMES
            ]
            tools = (
                single_read_tools
                + [dict(WORKER_TODO_TOOL_DEF)]
                + list(WRITE_TOOL_DEFS)
                + [dict(TERMINAL_TOOL_DEF)]
                + [dict(RUN_AND_WATCH_TOOL_DEF)]
                + list(GIT_TOOL_DEFS)
                + [dict(DIAGNOSTIC_TOOL_DEF)]
                + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]
                + [dict(WEB_SEARCH_TOOL_DEF)]
                + [dict(RUN_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(REGISTER_DRONE_FOLDER_TOOL_DEF)]
            )

        if not read_only and mode != "planner" and dynamic_schemas:
            tools.extend(dynamic_schemas)

        if mode != "planner" and mcp_schemas:
            tools.extend(mcp_schemas)

        return tools

    def build_replayable_tool_defs(
        self,
        *,
        mode: RegistryMode,
        read_only: bool,
    ) -> list[dict[str, Any]]:
        """Defs for tools this mode withholds but still accepts when called.

        A mode narrows its catalog to shape *choice*, not to revoke capability:
        the single-agent catalog drops the superseded read tools because
        offering all of them made the model pick an approach before it could
        pick an action, but their handlers stay registered so a replayed
        historical tool call still runs.  Preflight rejects any name outside
        the exposed catalog, so those withheld-but-callable names need their
        schemas from here or the replay is rejected instead of executed.

        Only observations qualify.  Nothing that can change the workspace is
        callable from outside the catalog that offered it, which is what keeps
        ``read_only`` and Planner mode's refusal to edit meaningful rather than
        advisory.
        """
        if read_only or mode != "single":
            return []
        return [
            tool for tool in READ_TOOL_DEFS
            if _tool_name(tool) in SINGLE_SUPERSEDED_READ_TOOL_NAMES
            and BUILTIN_TOOL_EFFECTS.get(_tool_name(tool)) is ToolEffect.OBSERVATION
        ]

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

    def build_focused_action_tool_defs(self) -> list[dict[str, Any]]:
        """Build the tool set for one focused action request.

        Exactly the production mutation tools already registered, plus
        ``report_blocker``. Nothing here is a replacement editing tool and
        nothing is invented: this is the normal write catalog with everything
        that invites another round of thinking — reads, search, research, TODO,
        git, terminal, diagnostics, drones, inspection, MCP, dynamic tools —
        left out, because the decision those tools serve has already been made.
        """
        mutation_tools = [
            copy.deepcopy(tool)
            for tool in WRITE_TOOL_DEFS
            if _tool_name(tool) in MUTATION_TOOL_NAMES
        ]
        return mutation_tools + [copy.deepcopy(REPORT_BLOCKER_TOOL_DEF)]
