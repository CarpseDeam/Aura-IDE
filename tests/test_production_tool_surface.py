"""The production SINGLE tool surface is minimal and instruction-free.

Two defects lived in the schema, not in the loop:

* the catalog offered a long tail of wrappers — ranked file recall, six git
  subcommand wrappers, a diagnostic runner, a workspace snapshot, drone tools —
  every one of them reachable through a tool that stayed, and every one of them
  another approach the model had to rule out before it could pick an action;
* the descriptions themselves taught inspection-first behaviour: use this
  before editing, use this to understand, verify before deciding, use this
  after editing.  A schema is not a place to put a workflow.

These tests read the *real* catalog the production registry exposes.  They test
what the model is actually shown, not a copy of it.
"""

from __future__ import annotations

import json

from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.registry import ToolRegistry

#: The whole ordinary production catalog: the capabilities a normal
#: implementation turn needs and nothing else.  ``web_search`` is not here — it
#: joins only for a turn whose route genuinely requires external research.
EXPECTED_PRODUCTION_TOOLS: frozenset[str] = frozenset({
    # read / search
    "read_file",
    "glob",
    "grep_search",
    # live plan
    "update_worker_todo",
    # write / edit
    "write_file",
    "patch_file",
    "delete_file",
    # structured terminal outcomes
    "report_blocker",
    "report_already_satisfied",
    # commands
    "run_terminal_command",
    "run_and_watch",
    # repository state
    "git_status",
    "git_diff",
    # progressive disclosure
    "load_skills",
    # Godot production capability (Veridea)
    "inspect_godot_assets",
    "inspect_godot_asset_preview",
    "capture_godot_asset_preview",
    "inspect_godot_api",
    "inspect_godot_editor",
    "edit_godot_scene",
    "edit_godot_editor",
    "edit_godot_asset_preview",
    "install_godot_editor_bridge",
})

#: Redundant surface removed from the production catalog. Each is reachable
#: through a tool that stayed, or is unrelated to normal implementation.
REMOVED_PRODUCTION_TOOLS: frozenset[str] = frozenset({
    "search_codebase",
    "read_files",
    "read_file_range",
    "read_file_outline",
    "read_task_context",
    "list_directory",
    "find_usages",
    "code_intel_outline",
    "code_intel_references",
    "code_intel_dependents",
    "code_intel_audit",
    "git_log",
    "git_show",
    "git_log_file",
    "git_branch_list",
    "git_stash_list",
    "git_stash_show",
    "run_diagnostic_command",
    "get_workspace_snapshot",
    "run_read_only_drone",
    "register_drone_folder",
})

#: Miniature workflows that must not appear in any production tool description.
#: Matched case-insensitively over the whole function schema, arguments
#: included — a coaching sentence hidden in a parameter description is the same
#: defect in a less visible place.
RETIRED_WORKFLOW_PHRASES: tuple[str, ...] = (
    "use this before editing",
    "before editing",
    "after editing",
    "use this to understand",
    "to understand what",
    "before planning",
    "before answering",
    "blast radius",
    "safe refactoring",
    "safe refactor",
    "verify before deciding",
    "before deciding",
    "inspect before",
    "before writing",
    "before making edits",
    "before finishing",
    "before dispatching",
    "authoritative owner",
    "discover the authoritative",
    "use after editing",
    "read the file first",
    "after reading the file",
    "re-read and retry",
    "you must run it",
    "before the first real file mutation",
    "use this at the start",
    "prefer run_diagnostic_command",
    "use search_codebase",
    "use read_file to verify",
    "inspect before and after",
    "inspect the preview afterward",
    "before live scene edits",
)


def _names(defs: list[dict]) -> list[str]:
    return [d["function"]["name"] for d in defs]


def _production_defs(*, web_search: bool = False) -> list[dict]:
    return ToolCatalog().build_tool_defs(
        mode="single", read_only=False, web_search=web_search
    )


# ── 1. the catalog is exactly the intended minimal set ──────────────────────


def test_production_schema_is_the_intended_minimal_tool_set() -> None:
    names = set(_names(_production_defs()))

    assert names == EXPECTED_PRODUCTION_TOOLS, {
        "unexpected": sorted(names - EXPECTED_PRODUCTION_TOOLS),
        "missing": sorted(EXPECTED_PRODUCTION_TOOLS - names),
    }
    assert not (names & REMOVED_PRODUCTION_TOOLS)


def test_the_live_registry_exposes_that_same_set(tmp_path) -> None:
    """The catalog is not tested in isolation from what production builds."""
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")

    assert set(_names(registry.tool_defs())) == EXPECTED_PRODUCTION_TOOLS


def test_removed_observations_stay_callable_for_transcript_replay(tmp_path) -> None:
    """Narrowing shapes choice; it does not revoke a replayed historical call."""
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")
    replayable = set(_names(registry.replayable_tool_defs()))

    for withheld in ("search_codebase", "read_files", "list_directory", "git_log"):
        assert withheld in replayable, withheld
    # Nothing that can change the workspace is callable from outside the
    # catalog that offered it.
    assert not (replayable & EXPECTED_PRODUCTION_TOOLS)


def test_planner_and_worker_catalogs_are_untouched() -> None:
    """This is a production-SINGLE narrowing, not a global capability cut."""
    catalog = ToolCatalog()

    planner = set(_names(catalog.build_tool_defs(mode="planner", read_only=False)))
    worker = set(_names(catalog.build_tool_defs(mode="worker", read_only=False)))

    assert "search_codebase" in planner
    assert {"git_log", "git_show", "git_log_file"} <= planner
    assert {"read_files", "list_directory"} <= worker


# ── 2. web_search is per-turn and stable within the turn ────────────────────


def test_web_search_joins_only_a_research_turn_and_nothing_else_moves() -> None:
    plain = _names(_production_defs(web_search=False))
    research = _names(_production_defs(web_search=True))

    assert "web_search" not in plain
    assert "web_search" in research
    # The rest of the catalog is byte-identical and in the same order, so a
    # research turn does not reshuffle the cached request prefix.
    assert [n for n in research if n != "web_search"] == plain


def test_the_registry_holds_the_web_search_decision_for_the_whole_turn(
    tmp_path,
) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="single")

    assert registry.web_search_enabled is False
    registry.set_web_search_enabled(True)
    first = registry.tool_defs()
    second = registry.tool_defs()

    assert "web_search" in _names(first)
    assert first == second, "the catalog moved between rounds of the same turn"


# ── 3. descriptions describe the tool, not a workflow ───────────────────────


def test_production_descriptions_contain_no_retired_workflow_phrases() -> None:
    offenders: dict[str, list[str]] = {}
    for tool in _production_defs(web_search=True):
        blob = json.dumps(tool["function"]).lower()
        hits = [p for p in RETIRED_WORKFLOW_PHRASES if p in blob]
        if hits:
            offenders[tool["function"]["name"]] = hits

    assert offenders == {}, offenders


def test_every_production_tool_still_describes_itself() -> None:
    """Stripping coaching must not leave a tool undocumented."""
    for tool in _production_defs(web_search=True):
        description = str(tool["function"].get("description") or "").strip()
        assert len(description) >= 80, tool["function"]["name"]


def test_the_production_schema_shrank() -> None:
    """Measured against the surface this repair was diagnosed on: 35 tools and
    35,768 characters of JSON."""
    defs = _production_defs(web_search=True)

    assert len(defs) <= 24
    assert len(json.dumps(defs)) < 30_000
