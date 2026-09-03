"""Authoritative runtime tool-effect model for exposed tools.

Every tool a model can call is classified into one of four effect classes so
runtime policy can reason about what a call may do without re-deriving intent
from the tool's name:

* ``observation``          — read-only inspection; no state change.
* ``mutation``             — changes workspace files or the open scene.
* ``command/validation``   — runs an external command or validation process.
* ``bookkeeping/control``  — records or controls conversation/tool state
  (TODO, dispatch, memory), not a user-visible edit.

The model is owned by the tool catalog and registry:

* Built-in production tools carry an explicit classification in
  ``BUILTIN_TOOL_EFFECTS``; ``ToolCatalog.effect_for`` raises for any built-in
  name that has none, so a newly exposed built-in cannot remain unclassified.
* Dynamic tools may declare ``AURA_TOOL_EFFECT`` in their script; MCP tools
  may declare ``x-aura-effect`` (or the standard MCP ``annotations``
  ``readOnlyHint``).

**Extensible tools fail safe.**  A dynamic or MCP tool that declares no valid
effect metadata — and any tool name the runtime does not recognise at all —
resolves to :data:`DEFAULT_EXTENSIBLE_TOOL_EFFECT`, a *consequential* effect,
never ``observation``.  These tools are third-party code the runtime cannot
inspect: a server tool called ``notion_delete_page`` is not read-only because
nobody annotated it, and treating silence as "harmless" is how an unannotated
tool slips past approval, past read-only mode, and past the loop guard's
progress accounting.  Observation is a claim that has to be made, not a default
that is assumed.

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


#: Effect assumed for any extensible tool without valid effect metadata, and
#: for any unrecognised tool name.  ``COMMAND`` rather than ``MUTATION``
#: because an unannotated tool most plausibly runs something on the other side
#: of a boundary the runtime cannot see; both are consequential, and that is
#: the property every caller relies on.  This constant is the single place the
#: fail-safe default is stated — resolution belongs to the registry layer.
DEFAULT_EXTENSIBLE_TOOL_EFFECT: ToolEffect = ToolEffect.COMMAND


#: Explicit classification for every built-in production tool.  The catalog
#: enumeration test fails if a built-in exposed by ``ToolCatalog`` is missing.
BUILTIN_TOOL_EFFECTS: dict[str, ToolEffect] = {
    # --- observation: read-only inspection, no state change ---
    "read_file": ToolEffect.OBSERVATION,
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
    "inspect_code": ToolEffect.OBSERVATION,
    "git_status": ToolEffect.OBSERVATION,
    "git_diff": ToolEffect.OBSERVATION,
    "git_log": ToolEffect.OBSERVATION,
    "git_show": ToolEffect.OBSERVATION,
    "git_log_file": ToolEffect.OBSERVATION,
    "git_branch_list": ToolEffect.OBSERVATION,
    "git_stash_list": ToolEffect.OBSERVATION,
    "git_stash_show": ToolEffect.OBSERVATION,
    "get_workspace_snapshot": ToolEffect.OBSERVATION,
    "list_agent_change_sets": ToolEffect.OBSERVATION,
    "inspect_agent_change_set": ToolEffect.OBSERVATION,
    "search_project_memory": ToolEffect.OBSERVATION,
    "load_skills": ToolEffect.OBSERVATION,
    "read_skill_resource": ToolEffect.OBSERVATION,
    # --- mutation: changes workspace files ---
    "apply_patch": ToolEffect.MUTATION,
    "apply_agent_change_set": ToolEffect.MUTATION,
    # --- command/validation: runs an external command or validation ---
    "shell": ToolEffect.COMMAND,
    "run_diagnostic_command": ToolEffect.COMMAND,
    # Foreground orchestration/control. It remains serialized by ToolRoundRunner
    # (only observations enter the parallel pool) without pretending a child
    # model call is a shell command.
    "delegate_agent": ToolEffect.BOOKKEEPING,
    "run_agent_workflow": ToolEffect.BOOKKEEPING,
    # --- bookkeeping/control: tracks or controls conversation/tool state ---
    "update_task_checklist": ToolEffect.BOOKKEEPING,
    "save_to_project_memory": ToolEffect.BOOKKEEPING,
    "review_implementation_plan": ToolEffect.BOOKKEEPING,
    "discard_agent_change_set": ToolEffect.BOOKKEEPING,
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
    metadata returns None, and the registry layer then applies the fail-safe
    :data:`DEFAULT_EXTENSIBLE_TOOL_EFFECT`.
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

    Returns None when the script declares no (valid) metadata, so the caller
    applies the fail-safe :data:`DEFAULT_EXTENSIBLE_TOOL_EFFECT`.
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
