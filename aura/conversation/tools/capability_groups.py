"""Deterministic capability groups for the production single-agent catalog.

Production runs in ``mode="single"``, which historically received the union of
nearly every tool Aura owns.  A one-file Python edit was therefore presented as
a choice among dozens of unrelated capabilities, rebuilt and resent on every
model round.

This module replaces universal exposure with *progressive* exposure:

* a small baseline that every ordinary implementation turn needs,
* additive groups admitted only when the turn's terrain calls for them,
* continuity — a group stays available for the rest of the turn once the model
  has actually used a tool from it.

Selection is deterministic and derives only from turn scope (task kind, the
files the user named, their own words) plus cheap workspace facts.  Nothing
here asks a model what it might need; that decision is exactly the deliberation
this is meant to remove.
"""

from __future__ import annotations

import re
from pathlib import Path

# --- Groups -----------------------------------------------------------------
#
# Every built-in single-mode tool belongs to exactly one group. A tool that is
# not listed here is never filtered (dynamic and MCP tools are appended after
# selection and keep their own gating).

CORE_READ = "core_read"
CORE_SEARCH = "core_search"
TODO = "todo"
EDIT = "edit"
TERMINAL = "terminal"
GIT_INSPECT = "git_inspect"
GIT_HISTORY = "git_history"
CODE_INTEL = "code_intel"
DIAGNOSTICS = "diagnostics"
SNAPSHOT = "snapshot"
WEB = "web"
DRONES = "drones"
GODOT = "godot"

CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    CORE_READ: frozenset({
        "read_file",
        "read_files",
        "read_file_range",
        "read_file_outline",
        "read_task_context",
        "list_directory",
        "glob",
    }),
    CORE_SEARCH: frozenset({
        "grep_search",
        "find_usages",
        "search_codebase",
    }),
    TODO: frozenset({"update_worker_todo"}),
    EDIT: frozenset({
        "write_file",
        "patch_file",
        "delete_file",
    }),
    TERMINAL: frozenset({
        "run_terminal_command",
        "run_and_watch",
    }),
    # Status and diff answer "what have I changed?" — the question an editing
    # turn actually asks. The rest of git is archaeology.
    GIT_INSPECT: frozenset({"git_status", "git_diff"}),
    GIT_HISTORY: frozenset({
        "git_log",
        "git_show",
        "git_log_file",
        "git_branch_list",
        "git_stash_list",
        "git_stash_show",
    }),
    CODE_INTEL: frozenset({
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
        "code_intel_audit",
    }),
    DIAGNOSTICS: frozenset({"run_diagnostic_command"}),
    SNAPSHOT: frozenset({"get_workspace_snapshot"}),
    WEB: frozenset({"web_search"}),
    DRONES: frozenset({
        "run_read_only_drone",
        "register_drone_folder",
    }),
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

# The floor for any turn that can touch code: inspect, search, plan, edit,
# run, and see what changed.
BASELINE: frozenset[str] = frozenset({
    CORE_READ,
    CORE_SEARCH,
    TODO,
    EDIT,
    TERMINAL,
    GIT_INSPECT,
})

# A turn Aura could not classify keeps everything except the two groups that
# are strictly terrain- or request-gated. Unknown scope is a reason to stay
# broad, not a reason to guess narrow.
_UNCLASSIFIED_EXTRA: frozenset[str] = frozenset({
    GIT_HISTORY,
    CODE_INTEL,
    DIAGNOSTICS,
    SNAPSHOT,
    WEB,
})

_CODING_TASK_KINDS = frozenset({
    "new tool or app",
    "bugfix",
    "gui polish",
    "cleanup",
    "refactor",
    "implementation",
})
_VALIDATION_TASK_KINDS = frozenset({"validation"})
_RESEARCH_TASK_KINDS = frozenset({
    "web_research",
    "research_then_worker",
    "answer_only",
})
# Task kinds where structural navigation earns its place in the catalog.
_STRUCTURAL_TASK_KINDS = frozenset({"refactor", "cleanup"})
_FAILURE_TASK_KINDS = frozenset({"bugfix", "validation"})

_GODOT_SUFFIXES = frozenset({".gd", ".tscn", ".tres", ".godot", ".gdshader", ".import"})

_GODOT_RE = re.compile(r"\b(?:godot|gdscript|gdshader|tscn|tres|scene\s+tree|node2d|node3d)\b")
_WEB_RE = re.compile(
    r"\b(?:search\s+the\s+web|web\s+search|google|look\s+up|latest|news|today|"
    r"documentation\s+for|changelog|release\s+notes|online)\b"
)
_DRONE_RE = re.compile(r"\bdrones?\b")
_SNAPSHOT_RE = re.compile(
    r"\b(?:snapshot|restore\s+point|restore|revert|roll\s*back|rollback|undo)\b"
)
_GIT_HISTORY_RE = re.compile(
    r"\b(?:git\s+log|commit|commits|blame|history|regression|bisect|stash|"
    r"branch|branches|when\s+did|who\s+changed|previous\s+version)\b"
)
_CODE_INTEL_RE = re.compile(
    r"\b(?:references?|usages?|callers?|call\s+sites?|dependents?|dependency|"
    r"dependencies|impact|audit|who\s+calls|outline)\b"
)
_FAILURE_RE = re.compile(
    r"\b(?:traceback|stack\s*trace|exception|failing|failed|failure|crash|"
    r"crashes|crashing|error|errors|broken)\b"
)


def capability_for_tool(tool_name: str) -> str | None:
    """The group a built-in tool belongs to, or None if it is ungrouped."""
    return _TOOL_CAPABILITY.get(tool_name)


def tool_names_for(capabilities: frozenset[str] | set[str]) -> frozenset[str]:
    """Every tool name reachable through the given groups."""
    names: set[str] = set()
    for capability in capabilities:
        names |= CAPABILITY_TOOLS.get(capability, frozenset())
    return frozenset(names)


def select_capabilities(
    *,
    task_kind: str | None,
    target_files: tuple[str, ...] = (),
    request_text: str = "",
    workspace_root: Path | None = None,
    used_tools: frozenset[str] | set[str] = frozenset(),
) -> frozenset[str]:
    """Decide which capability groups this turn may see.

    ``used_tools`` carries continuity: a group whose tool the model has already
    called this turn stays available, so exposure only ever grows within a turn
    and a half-finished workflow cannot lose the tool it was using.
    """
    normalized = (request_text or "").lower()
    kind = (task_kind or "").strip().lower() or None

    selected: set[str] = set(BASELINE)

    if kind is None:
        selected |= _UNCLASSIFIED_EXTRA
    elif kind in _RESEARCH_TASK_KINDS:
        # Research answers from the outside world; it still reads code to
        # ground the answer, but it is not an editing turn.
        selected.add(WEB)
    if kind in _STRUCTURAL_TASK_KINDS:
        selected.add(CODE_INTEL)
    if kind in _FAILURE_TASK_KINDS:
        selected.add(DIAGNOSTICS)
    if kind in _VALIDATION_TASK_KINDS:
        # Validation runs commands and reads results; keeping EDIT from the
        # baseline is deliberate, because single-agent mode repairs what its
        # own validation just caught.
        selected.add(GIT_HISTORY)

    if _WEB_RE.search(normalized):
        selected.add(WEB)
    if _DRONE_RE.search(normalized):
        selected.add(DRONES)
    if _SNAPSHOT_RE.search(normalized):
        selected.add(SNAPSHOT)
    if _GIT_HISTORY_RE.search(normalized):
        selected.add(GIT_HISTORY)
    if _CODE_INTEL_RE.search(normalized):
        selected.add(CODE_INTEL)
    if _FAILURE_RE.search(normalized):
        selected.add(DIAGNOSTICS)
    if _is_godot_terrain(normalized, target_files, workspace_root):
        selected.add(GODOT)

    for tool in used_tools:
        capability = capability_for_tool(tool)
        if capability is not None:
            selected.add(capability)

    return frozenset(selected)


def _is_godot_terrain(
    normalized_text: str,
    target_files: tuple[str, ...],
    workspace_root: Path | None,
) -> bool:
    """Whether this turn is plausibly about a Godot project.

    Three independent signals, cheapest first: a named Godot file, the user's
    own words, or a ``project.godot`` at the workspace root. The last one keeps
    an unqualified "fix the player movement" workable inside a Godot project
    without making every Python repo carry the scene tools.
    """
    for path in target_files:
        if Path(path).suffix.lower() in _GODOT_SUFFIXES:
            return True
    if _GODOT_RE.search(normalized_text):
        return True
    if workspace_root is not None:
        try:
            return (workspace_root / "project.godot").is_file()
        except OSError:
            return False
    return False


__all__ = [
    "BASELINE",
    "CAPABILITY_TOOLS",
    "capability_for_tool",
    "select_capabilities",
    "tool_names_for",
]
