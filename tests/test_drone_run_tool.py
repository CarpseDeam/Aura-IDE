from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry
from aura.drones.definition import DroneBudget, DroneDefinition


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def drone_store_dir(workspace: Path) -> Path:
    d = workspace / ".aura" / "drones"
    d.mkdir(parents=True)
    return d


def _save_read_only_drone(drone_dir: Path) -> str:
    """Save a read-only drone for testing. Returns drone_id."""
    import json
    from dataclasses import asdict

    drone = DroneDefinition(
        id="bug-scout",
        name="Bug Scout",
        description="Investigates bugs by searching code paths.",
        instructions="Search the codebase for the root cause of the reported issue.",
        write_policy="read_only",
        allowed_tools=("read_file", "grep_search", "search_codebase", "git_log", "git_diff", "git_status"),
        output_contract="Summary of findings with file paths and line numbers.",
        budget=DroneBudget(max_tool_rounds=5, timeout_seconds=120),
    )
    path = drone_dir / f"{drone.id}.json"
    path.write_text(json.dumps(asdict(drone), indent=2), encoding="utf-8")
    return drone.id


def _save_write_capable_drone(drone_dir: Path) -> str:
    """Save a write-capable drone for testing. Returns drone_id."""
    import json
    from dataclasses import asdict

    drone = DroneDefinition(
        id="writer-drone",
        name="Writer Drone",
        description="Can write files.",
        instructions="Write code for the user.",
        write_policy="ask_before_writes",
        allowed_tools=("read_file", "write_file", "edit_file"),
        output_contract="Generated code.",
        budget=DroneBudget(max_tool_rounds=5, timeout_seconds=120),
    )
    path = drone_dir / f"{drone.id}.json"
    path.write_text(json.dumps(asdict(drone), indent=2), encoding="utf-8")
    return drone.id


class TestRunReadOnlyDroneHandler:
    """Tests for _handle_run_read_only_drone."""

    def test_handler_registered(self):
        assert "run_read_only_drone" in TOOL_HANDLERS

    def test_rejects_unknown_drone(self, workspace: Path):
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "nonexistent", "goal": "find bugs"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is False
        payload_str = str(result.payload).lower()
        assert any(
            word in payload_str for word in ("unknown", "not found", "nonexistent")
        )

    def test_rejects_write_capable_drone(self, workspace: Path, drone_store_dir: Path):
        _save_write_capable_drone(drone_store_dir)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "writer-drone", "goal": "write code"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is False
        assert "write" in str(result.payload).lower() or "read_only" in str(result.payload).lower()

    def test_missing_drone_id(self, workspace: Path):
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is False

    def test_missing_goal(self, workspace: Path, drone_store_dir: Path):
        _save_read_only_drone(drone_store_dir)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "bug-scout", "goal": ""},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is False

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_valid_read_only_drone(self, mock_runner, workspace: Path, drone_store_dir: Path):
        _save_read_only_drone(drone_store_dir)
        mock_runner.return_value = {
            "ok": True,
            "run_id": "run123",
            "drone_id": "bug-scout",
            "drone_name": "Bug Scout",
            "status": "completed",
            "summary": "Found the bug in src/main.py line 42",
            "tool_calls_made": 3,
            "tool_errors": 0,
            "elapsed_seconds": 5.2,
        }
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "bug-scout", "goal": "find the crash bug"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is True
        assert "bug-scout" in str(result.payload)
        assert "Found the bug" in str(result.payload)
        mock_runner.assert_called_once()

    def test_per_turn_limit(self, workspace: Path, drone_store_dir: Path):
        _save_read_only_drone(drone_store_dir)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        # Exhaust the limit
        with patch("aura.drones.sync_runner.run_read_only_drone_sync") as mock_runner:
            mock_runner.return_value = {
                "ok": True,
                "run_id": "r1",
                "drone_id": "bug-scout",
                "drone_name": "Bug Scout",
                "status": "completed",
                "summary": "ok",
                "tool_calls_made": 1,
                "tool_errors": 0,
                "elapsed_seconds": 1.0,
            }
            r1 = registry.execute(
                "run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "find bugs"},
                MagicMock(return_value=ApprovalDecision(action="approve")),
                False,
            )
            assert r1.ok is True
            r2 = registry.execute(
                "run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "find more bugs"},
                MagicMock(return_value=ApprovalDecision(action="approve")),
                False,
            )
            assert r2.ok is True
            r3 = registry.execute(
                "run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "find even more"},
                MagicMock(return_value=ApprovalDecision(action="approve")),
                False,
            )
            assert r3.ok is False
            assert "limit" in str(r3.payload).lower()


class TestToolCatalogSurface:
    """Verify run_read_only_drone appears in mode tool lists."""

    def test_planner_has_run_read_only_drone(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="planner")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "run_read_only_drone" in tool_names

    def test_worker_has_run_read_only_drone(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="worker")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "run_read_only_drone" in tool_names

    def test_single_has_run_read_only_drone(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        registry = ToolRegistry(workspace_root=ws, read_only=False, mode="single")
        tool_names = {t["function"]["name"] for t in registry.tool_defs()}
        assert "run_read_only_drone" in tool_names
