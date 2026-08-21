"""The production tool surface is minimal: five built-ins, nothing more.

Phase 2C collapsed Aura's ordinary model-facing coding tools around one
persistent shell: a normal production turn presents exactly one route for
reading known content (``read_file``), one for repository search
(``grep_search``), one for filesystem edits (``apply_patch``), one for
commands (``shell``), and one simple visible TODO
(``update_task_checklist``). Everything else is either reachable through
``shell`` or joins only when its own activation condition is actually true
for this turn.

These tests read the *real* catalog the production registry exposes. They
test what the model is actually shown, not a copy of it.
"""

from __future__ import annotations

import json

from aura.conversation.tools.capability_groups import GODOT, tool_names_for
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.registry import ToolRegistry

#: The whole ordinary production catalog with no optional capability active.
EXPECTED_PRODUCTION_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "grep_search",
    "apply_patch",
    "shell",
    "update_task_checklist",
})

GODOT_TOOL_NAMES = tool_names_for({GODOT})

#: Redundant/removed surface: reachable through a tool that stayed (mostly
#: ``shell``), superseded, or unrelated to normal implementation. Handlers
#: stay registered so a replayed historical call still executes; none of
#: these are ever offered in the ordinary production catalog.
REMOVED_PRODUCTION_TOOLS: frozenset[str] = frozenset({
    "glob",
    "search_codebase",
    "inspect_code",
    "read_file_range",
    "read_file_outline",
    "read_task_context",
    "list_directory",
    "find_usages",
    "code_intel_outline",
    "code_intel_references",
    "code_intel_dependents",
    "code_intel_audit",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_log_file",
    "git_branch_list",
    "git_stash_list",
    "git_stash_show",
    "run_diagnostic_command",
    "run_and_watch",
    "get_workspace_snapshot",
    "run_read_only_drone",
    "register_drone_folder",
    "write_file",
    "patch_file",
    "delete_file",
    "report_blocker",
    "report_already_satisfied",
    "record_implementation_decision",
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


def _production_defs(**kwargs) -> list[dict]:
    return ToolCatalog().build_tool_defs(read_only=False, **kwargs)


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
    registry = ToolRegistry(workspace_root=tmp_path)

    assert set(_names(registry.tool_defs())) == EXPECTED_PRODUCTION_TOOLS


def test_regular_catalog_modes_expose_no_godot_capability_tools() -> None:
    for read_only in (False, True):
        names = set(
            _names(ToolCatalog().build_tool_defs(read_only=read_only))
        )
        assert not names & GODOT_TOOL_NAMES


def test_withheld_observations_are_absent_from_the_exposed_catalog(tmp_path) -> None:
    """A registered-but-withheld observation is not part of what gets sent.

    Phase 3A: the exact tool catalog sent with a request is the sole
    authority for which tool calls that response may execute, so a name
    absent here is not callable even though its handler stays registered
    (see ``registry.execute`` direct-call coverage elsewhere).
    """
    registry = ToolRegistry(workspace_root=tmp_path)
    exposed = set(_names(registry.tool_defs()))

    for withheld in (
        "find_usages", "list_directory", "git_log",
        "glob", "search_codebase", "inspect_code",
    ):
        assert withheld not in exposed, withheld


def test_read_files_tool_is_fully_removed(tmp_path) -> None:
    """``read_files`` was folded into ``read_file(paths=[...])``; the old
    standalone tool must be gone everywhere a model or replay could reach it."""
    from aura.conversation.tools.registry import TOOL_HANDLERS

    registry = ToolRegistry(workspace_root=tmp_path)

    assert "read_files" not in _names(registry.tool_defs())
    assert "read_files" not in TOOL_HANDLERS


def test_legacy_write_tool_names_are_not_in_any_catalog(tmp_path) -> None:
    """The consolidated names are gone from the exposed surface."""
    registry = ToolRegistry(workspace_root=tmp_path)
    legacy = {"write_file", "patch_file", "delete_file"}

    assert not (set(_names(registry.tool_defs())) & legacy)


def test_catalog_has_no_runtime_mode_dimension() -> None:
    """The production catalog is derived from actual capability facts only."""
    catalog = ToolCatalog()
    names = set(_names(catalog.build_tool_defs(read_only=False)))

    assert not (names & {"git_log", "git_show", "git_log_file", "list_directory"})


def test_tool_order_and_schemas_are_stable_across_rounds(tmp_path) -> None:
    """Nothing here re-derives a different catalog on a later round of one turn."""
    registry = ToolRegistry(workspace_root=tmp_path)

    first = registry.tool_defs()
    second = registry.tool_defs()

    assert first == second


# ── 2. optional capabilities join only under their explicit conditions ──────


def test_web_search_joins_only_a_research_turn_and_nothing_else_moves() -> None:
    plain = _names(_production_defs(web_search=False))
    research = _names(_production_defs(web_search=True))

    assert "web_search" not in plain
    assert "web_search" in research
    # The rest of the catalog is byte-identical and in the same order, so a
    # research turn does not reshuffle the cached request prefix.
    assert [n for n in research if n != "web_search"] == plain


def test_the_registry_holds_the_web_search_decision_for_the_whole_turn(
    tmp_path, monkeypatch,
) -> None:
    """Availability resolves from configuration once; rounds never re-resolve."""
    registry = ToolRegistry(workspace_root=tmp_path)

    assert registry.web_search_enabled is False
    assert "web_search" not in _names(registry.tool_defs())

    # The backend is configured: the turn's catalog offers web research.
    monkeypatch.setattr("aura.config.has_api_key", lambda provider_id: True)
    registry.refresh_web_search_availability()

    first = registry.tool_defs()
    second = registry.tool_defs()

    assert "web_search" in _names(first)
    assert first == second, "the catalog moved between rounds of the same turn"
    # tool_defs() never re-resolves availability mid-turn: even a config
    # change only takes effect at the next turn's explicit refresh.
    monkeypatch.setattr("aura.config.has_api_key", lambda provider_id: False)
    assert "web_search" in _names(registry.tool_defs())


def test_load_skills_joins_only_when_the_turn_has_frozen_candidates(tmp_path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)

    assert "load_skills" not in _names(registry.tool_defs())

    class _FakeSkillTurnState:
        is_empty = False

    registry.set_turn_skill_state(_FakeSkillTurnState())
    assert "load_skills" in _names(registry.tool_defs())

    registry.set_turn_skill_state(None)
    assert "load_skills" not in _names(registry.tool_defs())


def test_plan_review_joins_only_when_required_and_never_read_only(tmp_path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    assert "review_implementation_plan" not in _names(registry.tool_defs())

    registry.plan_review.begin_turn(required=True)
    assert "review_implementation_plan" in _names(registry.tool_defs())

    registry.set_read_only(True)
    assert "review_implementation_plan" not in _names(registry.tool_defs())


def test_reference_tool_joins_only_when_a_reference_is_authorized(tmp_path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path)
    assert "read_reference_file" not in _names(registry.tool_defs())


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
        assert len(description) >= 40, tool["function"]["name"]


def test_the_production_schema_shrank() -> None:
    """The production surface stays small now that it is five tools plus options."""
    defs = _production_defs(web_search=True)

    assert len(defs) <= 8
    assert len(json.dumps(defs)) < 12_000
