"""Inventory of the built-in tools, grouped by capability.

This is a map, not a policy. Nothing here decides what a turn may see — the
production catalog is fixed and identical on every round, because tool
definitions live in the provider's cached request prefix and a catalog that
changes between rounds throws that cache away.

The grouping exists so the catalog can be reasoned about and staged:

* ``CORE_*`` groups are what production exposes today,
* ``SPECIALIZED`` groups are the long tail — the natural contents of a
  name-only deferred index, should Aura grow one.

Keep this in sync with the schemas in ``_schemas.py`` and ``_drone_schemas.py``.
"""

from __future__ import annotations

CORE_READ = "core_read"
CORE_SEARCH = "core_search"
CORE_EDIT = "core_edit"
CORE_TERMINAL = "core_terminal"
CORE_TODO = "core_todo"
CORE_WEB = "core_web"

GIT = "git"
CODE_INTEL = "code_intel"
INSPECT = "inspect"
BULK_READ = "bulk_read"
DIAGNOSTICS = "diagnostics"
SNAPSHOT = "snapshot"
DRONES = "drones"
GODOT = "godot"

CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    CORE_READ: frozenset({"read_file", "glob"}),
    # ``grep_search`` is exact lexical/regex matching. ``search_codebase`` is
    # ranked conceptual/keyword retrieval over structure-aware retrieval
    # documents — bounded source regions with symbol/kind/parent metadata,
    # not whole files. Both are exposed in the production catalog.
    CORE_SEARCH: frozenset({"grep_search", "search_codebase"}),
    CORE_EDIT: frozenset({"write_file", "patch_file", "delete_file"}),
    CORE_TERMINAL: frozenset({"run_terminal_command", "run_and_watch"}),
    CORE_TODO: frozenset({"update_worker_todo"}),
    CORE_WEB: frozenset({"web_search"}),
    # Superseded by read_file's offset/limit and by parallel read_file calls.
    BULK_READ: frozenset({
        "read_files",
        "read_file_range",
        "read_file_outline",
        "read_task_context",
        "list_directory",
    }),
    # Superseded by grep_search and read_file.
    CODE_INTEL: frozenset({
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
        "code_intel_audit",
    }),
    # The single production semantic-inspection tool that replaces the
    # CODE_INTEL group above in the production catalog.
    INSPECT: frozenset({"inspect_code"}),
    GIT: frozenset({
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_log_file",
        "git_branch_list",
        "git_stash_list",
        "git_stash_show",
    }),
    DIAGNOSTICS: frozenset({"run_diagnostic_command"}),
    SNAPSHOT: frozenset({"get_workspace_snapshot"}),
    DRONES: frozenset({"run_read_only_drone", "register_drone_folder"}),
    GODOT: frozenset({
        "inspect_godot_assets",
        "inspect_godot_asset_preview",
        "capture_godot_asset_preview",
        "inspect_godot_api",
        "inspect_godot_editor",
        "edit_godot_scene",
        "edit_godot_editor",
        "edit_godot_asset_preview",
        "install_godot_editor_bridge",
    }),
}

_TOOL_CAPABILITY: dict[str, str] = {
    tool: capability
    for capability, tools in CAPABILITY_TOOLS.items()
    for tool in tools
}


def capability_for_tool(tool_name: str) -> str | None:
    """The group a built-in tool belongs to, or None if it is not built in."""
    return _TOOL_CAPABILITY.get(tool_name)


def tool_names_for(capabilities: frozenset[str] | set[str]) -> frozenset[str]:
    """Every tool name reachable through the given groups."""
    names: set[str] = set()
    for capability in capabilities:
        names |= CAPABILITY_TOOLS.get(capability, frozenset())
    return frozenset(names)


__all__ = [
    "CAPABILITY_TOOLS",
    "capability_for_tool",
    "tool_names_for",
]
