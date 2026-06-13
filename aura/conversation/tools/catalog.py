"""Tool catalog — builds the list of tool schemas for the current mode/read-only state."""

from __future__ import annotations

from typing import Any

from aura.conversation.tools._schemas import (
    CHECK_DRONE_RUN_TOOL_DEF,
    DIAGNOSTIC_TOOL_DEF,
    DISPATCH_TOOL_DEF,
    GIT_TOOL_DEFS,
    LAUNCH_READ_ONLY_DRONE_TOOL_DEF,
    READ_TOOL_DEFS,
    RESEARCH_TOOL_DEFS,
    RESOLVE_CAPABILITY_TOOL_DEF,
    RUN_READ_ONLY_DRONE_TOOL_DEF,
    SAVE_DRONE_DEFINITION_TOOL_DEF,
    SUMMON_DRONE_TOOL_DEF,
    TERMINAL_TOOL_DEF,
    WEB_TOOL_DEFS,
    WORKER_TODO_TOOL_DEF,
    WORKSPACE_SNAPSHOT_TOOL_DEF,
    WRITE_TOOL_DEFS,
)
from aura.conversation.tools._types import RegistryMode

PLANNER_TOOL_NAMES = {
    "read_file",
    "read_files",
    "read_file_range",
    "read_file_outline",
    "list_directory",
    "glob",
    "grep_search",
    "find_usages",
    "search_codebase",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_log_file",
}

NORMAL_WORKER_WRITE_TOOL_NAMES = {"write_file", "patch_file", "delete_file"}


def _tool_name(tool_def: dict[str, Any]) -> str:
    fn = tool_def.get("function")
    return str(fn.get("name", "")) if isinstance(fn, dict) else ""


class ToolCatalog:
    """Builds the list of available tool schemas for the current mode/read-only state.

    Given mode, read-only state, dynamic schemas, and MCP schemas,
    returns the complete list of tool definitions for the API.
    """

    def build_tool_defs(
        self,
        *,
        mode: RegistryMode,
        read_only: bool,
        dynamic_schemas: list[dict[str, Any]] | None = None,
        mcp_schemas: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build tool definitions for the given mode and state.

        Args:
            mode: The current registry mode.
            read_only: If True, only read and git tools are returned.
            dynamic_schemas: Schemas from dynamic (user) tools.
            mcp_schemas: Schemas from connected MCP servers.

        Returns:
            A list of OpenAI-compatible tool definition dicts.
        """
        # Read-only is the safety floor — strips writes AND dispatch (since
        # there's nothing for a worker to do without writes).
        if read_only:
            tools: list[dict[str, Any]] = list(READ_TOOL_DEFS) + list(GIT_TOOL_DEFS)
        elif mode == "researcher":
            tools = list(WEB_TOOL_DEFS) + list(READ_TOOL_DEFS)
        elif mode == "planner":
            planner_read_tools = [
                tool for tool in READ_TOOL_DEFS if _tool_name(tool) in PLANNER_TOOL_NAMES
            ]
            planner_git_tools = [
                tool for tool in GIT_TOOL_DEFS if _tool_name(tool) in PLANNER_TOOL_NAMES
            ]
            tools = (
                planner_read_tools
                + planner_git_tools
                + [dict(DISPATCH_TOOL_DEF)]
                + [dict(SUMMON_DRONE_TOOL_DEF)]
                + [dict(LAUNCH_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(RUN_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(CHECK_DRONE_RUN_TOOL_DEF)]
                + list(RESEARCH_TOOL_DEFS)
                + [dict(DIAGNOSTIC_TOOL_DEF)]
                + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]
                + [dict(RESOLVE_CAPABILITY_TOOL_DEF)]
            )
        elif mode == "worker":
            worker_write_tools = [
                tool for tool in WRITE_TOOL_DEFS
                if _tool_name(tool) in NORMAL_WORKER_WRITE_TOOL_NAMES
            ]
            tools = (
                list(READ_TOOL_DEFS)
                + worker_write_tools
                + [dict(WORKER_TODO_TOOL_DEF)]
                + [dict(TERMINAL_TOOL_DEF)]
                + list(GIT_TOOL_DEFS)
                + list(RESEARCH_TOOL_DEFS)
                + [dict(LAUNCH_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(RUN_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(CHECK_DRONE_RUN_TOOL_DEF)]
                + [dict(SAVE_DRONE_DEFINITION_TOOL_DEF)]
            )
        else:  # "single" or any unknown mode
            tools = (
                list(READ_TOOL_DEFS)
                + list(WRITE_TOOL_DEFS)
                + [dict(TERMINAL_TOOL_DEF)]
                + list(GIT_TOOL_DEFS)
                + list(RESEARCH_TOOL_DEFS)
                + [dict(DIAGNOSTIC_TOOL_DEF)]
                + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]
                + [dict(RUN_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(SAVE_DRONE_DEFINITION_TOOL_DEF)]
            )

        # Append dynamic tools (only when not read-only)
        if not read_only and mode != "planner" and dynamic_schemas:
            tools.extend(dynamic_schemas)

        # Append MCP tool schemas outside planner mode. Planner keeps a small,
        # non-mutating dispatch surface; workers/single mode retain extensions.
        if mode != "planner" and mcp_schemas:
            tools.extend(mcp_schemas)

        return tools
