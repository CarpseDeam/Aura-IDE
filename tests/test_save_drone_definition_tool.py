"""Tests for the save_drone_definition Worker tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from aura.conversation.tools._types import (
    ApprovalRequest,
    ApprovalDecision,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.drones.build_spec import DroneBuildBrief
from aura.drones.build_prompt import build_drone_creation_prompt
from aura.drones.store import DroneStore


def _noop_approval(req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="approve", note="")


def test_save_drone_definition_creates_and_loads_drone(tmp_path: Path) -> None:
    """Call save_drone_definition, verify it creates a drone and it can be loaded."""
    registry = ToolRegistry(workspace_root=tmp_path, mode="worker")
    args = {
        "name": "Test Drone",
        "description": "A drone for testing",
        "instructions": "You are a test drone.",
        "write_policy": "read_only",
        "allowed_tools": ["read_file", "grep_search"],
        "output_contract": "Return a report.",
        "scope": "global",
        "enabled": True,
        "created_by": "test",
        "budget": {"max_tool_rounds": 5, "timeout_seconds": 60},
    }
    result = registry.execute("save_drone_definition", args, _noop_approval)
    assert result.ok is True, f"Expected ok=True, got {result.payload}"
    assert result.extras.get("drone_saved") is True
    assert result.extras.get("drone_id") is not None
    assert result.payload.get("id") == result.extras.get("drone_id")

    drone_id = result.extras["drone_id"]
    loaded = DroneStore.load_drone(tmp_path, drone_id)
    assert loaded is not None, "Drone should be loadable after save"
    assert loaded.name == "Test Drone"
    assert loaded.description == "A drone for testing"
    assert loaded.instructions == "You are a test drone."
    assert loaded.write_policy == "read_only"
    assert loaded.allowed_tools == ("read_file", "grep_search")
    assert loaded.output_contract == "Return a report."
    assert loaded.scope == "global"
    assert loaded.enabled is True
    assert loaded.created_by == "test"
    assert loaded.budget.max_tool_rounds == 5
    assert loaded.budget.timeout_seconds == 60


def test_worker_catalog_includes_save_drone_definition(tmp_path: Path) -> None:
    """Worker mode tool_defs should include save_drone_definition."""
    registry = ToolRegistry(workspace_root=tmp_path, mode="worker")
    defs = registry.tool_defs()
    names = [d["function"]["name"] for d in defs]
    assert "save_drone_definition" in names


def test_planner_catalog_excludes_save_drone_definition(tmp_path: Path) -> None:
    """Planner mode tool_defs should NOT include save_drone_definition."""
    registry = ToolRegistry(workspace_root=tmp_path, mode="planner")
    defs = registry.tool_defs()
    names = [d["function"]["name"] for d in defs]
    assert "save_drone_definition" not in names


def test_build_drone_creation_prompt_forbids_scripts() -> None:
    """build_drone_creation_prompt should forbid scripts and mention save_drone_definition."""
    brief = DroneBuildBrief(
        response_type="brief",
        message="Test message",
        ready_to_build=True,
        build_brief="Test build brief",
    )
    prompt = build_drone_creation_prompt(brief)

    # Must mention save_drone_definition
    assert "save_drone_definition" in prompt

    # Must explicitly say no scripts/helper files
    assert "DO NOT create" in prompt

    # Must reference the compiled build plan
    assert "Compiled Build Plan" in prompt
    assert "allowed_tools" in prompt
    assert "Build Brief" in prompt

    # Must NOT reference definition.py schema files directly
    assert "definition.py" not in prompt
