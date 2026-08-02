"""Authoritative runtime tool-effect model for exposed tools.

Every tool a model can call is classified into one of four effect classes so
runtime policy can reason about what a call may do without re-deriving intent
from the tool's name:

* ``observation``          — read-only inspection; no state change.
* ``mutation``             — changes workspace files or the open scene.
* ``command/validation``   — runs an external command or validation process.
* ``bookkeeping/control``  — records or controls conversation/tool state
  (TODO, dispatch, memory, drone registration), not a user-visible edit.

The model is owned by the tool catalog and registry:

* Built-in production tools carry an explicit classification in
  ``BUILTIN_TOOL_EFFECTS``; ``ToolCatalog.effect_for`` raises for any built-in
  name that has none, so a newly exposed built-in cannot remain unclassified.
* Dynamic tools may declare ``AURA_TOOL_EFFECT`` in their script; MCP tools
  may declare ``x-aura-effect`` (or the standard MCP ``annotations``
  ``readOnlyHint``).  Anything without declared metadata is ``observation``.

This model intentionally does not reuse the consequential-approval
name-prefix heuristic (``is_consequential``): classification is explicit and
owned here, not derived from verb prefixes at call time.
"""

from __future__ import annotations

import ast
import enum
from pathlib import Path
from typing import Any

#: Metadata key a dynamic tool script may declare (module-level constant).
DYNAMIC_EFFECT_METADATA_NAME = "AURA_TOOL_EFFECT"

#: Metadata key an MCP tool definition may carry to declare its effect.
SCHEMA_EFFECT_KEY = "x-aura-effect"


class ToolEffect(enum.Enum):
    """Effect classes for exposed tools."""

    OBSERVATION = "observation"
    MUTATION = "mutation"
    COMMAND = "command/validation"
    BOOKKEEPING = "bookkeeping/control"


#: Explicit classification for every built-in production tool.  The catalog
#: enumeration test fails if a built-in exposed by ``ToolCatalog`` is missing.
BUILTIN_TOOL_EFFECTS: dict[str, ToolEffect] = {
    # --- observation: read-only inspection, no state change ---
    "inspect_godot_assets": ToolEffect.OBSERVATION,
    "inspect_godot_asset_preview": ToolEffect.OBSERVATION,
    "capture_godot_asset_preview": ToolEffect.OBSERVATION,
    "inspect_godot_api": ToolEffect.OBSERVATION,
    "inspect_godot_editor": ToolEffect.OBSERVATION,
    "read_file": ToolEffect.OBSERVATION,
    "read_files": ToolEffect.OBSERVATION,
    "read_file_range": ToolEffect.OBSERVATION,
    "read_file_outline": ToolEffect.OBSERVATION,
    "read_task_context": ToolEffect.OBSERVATION,
    "list_directory": ToolEffect.OBSERVATION,
    "glob": ToolEffect.OBSERVATION,
    "grep_search": ToolEffect.OBSERVATION,
    "find_usages": ToolEffect.OBSERVATION,
    "search_codebase": ToolEffect.OBSERVATION,
    "code_intel_outline": ToolEffect.OBSERVATION,
    "code_intel_references": ToolEffect.OBSERVATION,
    "code_intel_dependents": ToolEffect.OBSERVATION,
    "code_intel_audit": ToolEffect.OBSERVATION,
    "git_status": ToolEffect.OBSERVATION,
    "git_diff": ToolEffect.OBSERVATION,
    "git_log": ToolEffect.OBSERVATION,
    "git_show": ToolEffect.OBSERVATION,
    "git_log_file": ToolEffect.OBSERVATION,
    "git_branch_list": ToolEffect.OBSERVATION,
    "git_stash_list": ToolEffect.OBSERVATION,
    "git_stash_show": ToolEffect.OBSERVATION,
    "web_search": ToolEffect.OBSERVATION,
    "get_workspace_snapshot": ToolEffect.OBSERVATION,
    "search_project_memory": ToolEffect.OBSERVATION,
    "launch_read_only_drone": ToolEffect.OBSERVATION,
    "run_read_only_drone": ToolEffect.OBSERVATION,
    "check_drone_run": ToolEffect.OBSERVATION,
    # --- mutation: changes workspace files or the open scene ---
    "write_file": ToolEffect.MUTATION,
    "patch_file": ToolEffect.MUTATION,
    "delete_file": ToolEffect.MUTATION,
    "edit_godot_scene": ToolEffect.MUTATION,
    "edit_godot_editor": ToolEffect.MUTATION,
    "edit_godot_asset_preview": ToolEffect.MUTATION,
    "install_godot_editor_bridge": ToolEffect.MUTATION,
    # --- command/validation: runs an external command or validation ---
    "run_terminal_command": ToolEffect.COMMAND,
    "run_and_watch": ToolEffect.COMMAND,
    "run_diagnostic_command": ToolEffect.COMMAND,
    # --- bookkeeping/control: tracks or controls conversation/tool state ---
    "update_worker_todo": ToolEffect.BOOKKEEPING,
    "report_blocker": ToolEffect.BOOKKEEPING,
    "dispatch_to_worker": ToolEffect.BOOKKEEPING,
    "summon_drone": ToolEffect.BOOKKEEPING,
    "register_drone_folder": ToolEffect.BOOKKEEPING,
    "declare_ui_contract": ToolEffect.BOOKKEEPING,
    "save_to_project_memory": ToolEffect.BOOKKEEPING,
}

_EFFECT_BY_VALUE: dict[str, ToolEffect] = {effect.value: effect for effect in ToolEffect}
_ALIASES: dict[str, ToolEffect] = {
    "command": ToolEffect.COMMAND,
    "validation": ToolEffect.COMMAND,
    "bookkeeping": ToolEffect.BOOKKEEPING,
    "control": ToolEffect.BOOKKEEPING,
}


def effect_from_metadata(value: Any) -> ToolEffect | None:
    """Resolve declared effect metadata to a ToolEffect, or None.

    Accepts a string effect name (``"mutation"``, ``"command/validation"``,
    ...) or a mapping with an ``effect``/``class`` key.  Unknown or missing
    metadata returns None so callers apply the observation default.
    """
    if isinstance(value, dict):
        value = value.get("effect", value.get("class"))
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    effect = _EFFECT_BY_VALUE.get(key)
    if effect is None:
        effect = _ALIASES.get(key)
    return effect


def parse_dynamic_effect_metadata(file_path: Path) -> ToolEffect | None:
    """Parse a dynamic tool script's ``AURA_TOOL_EFFECT`` declaration.

    Returns None when the script declares no (valid) metadata so the caller
    applies the observation default.
    """
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except (OSError, SyntaxError, UnicodeError):
        return None

    for node in tree.body:
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == DYNAMIC_EFFECT_METADATA_NAME
            for target in node.targets
        ):
            value_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == DYNAMIC_EFFECT_METADATA_NAME
        ):
            value_node = node.value
        if value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            return None
        return effect_from_metadata(value)
    return None
