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
    RUN_AND_WATCH_TOOL_DEF,
    SUMMON_DRONE_TOOL_DEF,
    TERMINAL_TOOL_DEF,
    WORKER_TODO_TOOL_DEF,
    WORKSPACE_SNAPSHOT_TOOL_DEF,
    WRITE_TOOL_DEFS,
)
from aura.conversation.tools._types import RegistryMode

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
    "inspect_godot_editor",
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
    "install_godot_editor_bridge",
}


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
            tools = (
                list(READ_TOOL_DEFS)
                + list(WRITE_TOOL_DEFS)
                + [dict(TERMINAL_TOOL_DEF)]
                + [dict(RUN_AND_WATCH_TOOL_DEF)]
                + list(GIT_TOOL_DEFS)
                + [dict(DIAGNOSTIC_TOOL_DEF)]
                + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]
                + [dict(RUN_READ_ONLY_DRONE_TOOL_DEF)]
                + [dict(REGISTER_DRONE_FOLDER_TOOL_DEF)]
            )

        if not read_only and mode != "planner" and dynamic_schemas:
            tools.extend(dynamic_schemas)

        if mode != "planner" and mcp_schemas:
            tools.extend(mcp_schemas)

        return tools
