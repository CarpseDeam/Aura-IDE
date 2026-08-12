"""Regression coverage for built-in schema ownership and stable ordering."""

from __future__ import annotations

from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.schemas import GIT_TOOL_DEFS, READ_TOOL_DEFS, WRITE_TOOL_DEFS


def _names(defs: list[dict]) -> list[str]:
    return [tool["function"]["name"] for tool in defs]


def test_aggregate_schema_order_matches_the_existing_model_surface() -> None:
    assert _names(READ_TOOL_DEFS) == [
        "inspect_godot_assets",
        "inspect_godot_asset_preview",
        "capture_godot_asset_preview",
        "inspect_godot_api",
        "inspect_godot_editor",
        "read_file",
        "read_files",
        "read_task_context",
        "list_directory",
        "glob",
        "read_file_outline",
        "read_file_range",
        "grep_search",
        "find_usages",
        "search_codebase",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
        "code_intel_audit",
    ]
    assert _names(WRITE_TOOL_DEFS) == [
        "install_godot_editor_bridge",
        "edit_godot_editor",
        "edit_godot_asset_preview",
        "edit_godot_scene",
        "write_file",
        "delete_file",
        "patch_file",
    ]
    assert _names(GIT_TOOL_DEFS) == [
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_log_file",
        "git_branch_list",
        "git_stash_list",
        "git_stash_show",
    ]


def test_catalog_composition_preserves_read_only_and_single_order() -> None:
    catalog = ToolCatalog()
    read_only = _names(catalog.build_tool_defs(mode="single", read_only=True))
    single = _names(catalog.build_tool_defs(mode="single", read_only=False))

    assert read_only == _names(READ_TOOL_DEFS) + [
        "load_skills",
    ] + _names(GIT_TOOL_DEFS)
    assert single == [
        "inspect_godot_assets",
        "inspect_godot_asset_preview",
        "capture_godot_asset_preview",
        "inspect_godot_api",
        "inspect_godot_editor",
        "read_file",
        "glob",
        "grep_search",
        "search_codebase",
        "inspect_code",
        "update_worker_todo",
        "record_implementation_decision",
        "install_godot_editor_bridge",
        "edit_godot_editor",
        "edit_godot_asset_preview",
        "edit_godot_scene",
        "write_file",
        "delete_file",
        "patch_file",
        "report_blocker",
        "report_already_satisfied",
        "run_terminal_command",
        "run_and_watch",
        "git_status",
        "git_diff",
        "load_skills",
    ]
