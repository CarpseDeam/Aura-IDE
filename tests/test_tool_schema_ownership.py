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
        "apply_patch",
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


def test_catalog_composition_preserves_read_only_and_production_order() -> None:
    catalog = ToolCatalog()
    read_only = _names(catalog.build_tool_defs(read_only=True))
    production = _names(catalog.build_tool_defs(read_only=False))

    # Read-only: the compact observation surface (read_file, glob, grep_search
    # -- in READ_TOOL_DEFS's own relative order) plus every git tool. No
    # optional capability (load_skills, plan review, ...) is active by default.
    assert read_only == ["read_file", "glob", "grep_search"] + _names(GIT_TOOL_DEFS)

    # Production: exactly the five built-ins, in build order. No optional
    # capability is active by default either.
    assert production == [
        "read_file",
        "grep_search",
        "update_task_checklist",
        "apply_patch",
        "shell",
    ]
