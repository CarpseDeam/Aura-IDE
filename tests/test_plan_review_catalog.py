"""Plan Review catalog exposure: only when active, never in read-only mode."""
from __future__ import annotations

from pathlib import Path

from aura.conversation.tools.registry import ToolRegistry

TOOL_NAME = "review_implementation_plan"


def _tool_names(tool_defs: list[dict]) -> set[str]:
    names: set[str] = set()
    for tool_def in tool_defs:
        fn = tool_def.get("function") or {}
        name = fn.get("name")
        if name:
            names.add(str(name))
    return names


def test_production_catalog_omits_review_tool_by_default(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    assert TOOL_NAME not in _tool_names(registry.tool_defs())


def test_production_catalog_exposes_review_tool_when_plan_review_required(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    registry.plan_review.begin_turn(required=True)
    assert TOOL_NAME in _tool_names(registry.tool_defs())


def test_read_only_catalog_never_exposes_review_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, read_only=True)
    registry.plan_review.begin_turn(required=True)
    assert TOOL_NAME not in _tool_names(registry.tool_defs())

    # Even switching out of read-only mid-object, the *frozen* per-turn
    # required flag still governs exposure — this only proves read-only
    # itself withholds the tool regardless of that flag.
    registry.set_read_only(False)
    assert TOOL_NAME in _tool_names(registry.tool_defs())


def test_catalog_exposure_tracks_the_frozen_turn_flag_not_a_live_toggle(tmp_path: Path) -> None:
    """Exposure follows ``plan_review.required`` exactly as frozen for the turn.

    ``begin_turn`` is what a real turn boundary (``ConversationBridge.send``)
    calls; a mid-turn toolbar flip must not retroactively change the catalog
    already committed to that turn (proven at the state level in
    ``test_plan_review_tool_flow.py``). Here we only prove the catalog
    reflects whatever the frozen flag currently says.
    """
    registry = ToolRegistry(tmp_path)
    registry.plan_review.begin_turn(required=True)
    assert TOOL_NAME in _tool_names(registry.tool_defs())
    registry.plan_review.begin_turn(required=False)
    assert TOOL_NAME not in _tool_names(registry.tool_defs())
