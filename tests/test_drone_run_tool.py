from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura import paths as aura_paths
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry
from aura.drones.store import DroneStore


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("aura.drones.store.data_dir", lambda: tmp_path / "data")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _register_drone(workspace: Path, *, write_policy: str = "read_only") -> str:
    drone_id = "bug-scout" if write_policy == "read_only" else "writer-drone"
    folder = workspace / "build" / drone_id
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(
        "def run(payload):\n"
        "    return {'summary': payload.get('goal')}\n",
        encoding="utf-8",
    )
    (folder / "smoke.py").write_text(
        "def run(payload):\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": drone_id,
                "name": "Bug Scout" if write_policy == "read_only" else "Writer Drone",
                "description": "Investigates bugs.",
                "runtime": "python",
                "entrypoint": "main:run",
                "smoke": "smoke:run",
                "instructions": "Run the entrypoint.",
                "write_policy": write_policy,
                "output_contract": "Return cargo.",
            }
        ),
        encoding="utf-8",
    )
    DroneStore.register_drone_folder(workspace, folder)
    return drone_id


class TestRunReadOnlyDroneHandler:
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
        assert "no drone found" in str(result.payload).lower()

    def test_rejects_write_capable_drone(self, workspace: Path):
        _register_drone(workspace, write_policy="ask_before_writes")
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "writer-drone", "goal": "write code"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is False
        assert "read-only" in str(result.payload).lower() or "read_only" in str(result.payload).lower()

    def test_missing_drone_id(self, workspace: Path):
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is False

    def test_missing_goal(self, workspace: Path):
        _register_drone(workspace)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "bug-scout", "goal": ""},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is False

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_valid_read_only_drone(self, mock_runner, workspace: Path):
        _register_drone(workspace)
        mock_runner.return_value = {
            "ok": True,
            "run_id": "run123",
            "drone_id": "bug-scout",
            "drone_name": "Bug Scout",
            "status": "completed",
            "summary": "Found the bug",
            "tool_calls_made": 0,
            "tool_errors": 0,
            "elapsed_seconds": 0.1,
        }
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "bug-scout", "goal": "find the crash bug"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )
        assert result.ok is True
        assert "Found the bug" in str(result.payload)
        mock_runner.assert_called_once()

    def test_per_turn_limit(self, workspace: Path):
        _register_drone(workspace)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        with patch("aura.drones.sync_runner.run_read_only_drone_sync") as mock_runner:
            mock_runner.return_value = {
                "ok": True,
                "run_id": "r1",
                "drone_id": "bug-scout",
                "drone_name": "Bug Scout",
                "status": "completed",
                "summary": "ok",
                "tool_calls_made": 0,
                "tool_errors": 0,
                "elapsed_seconds": 0.1,
            }
            assert registry.execute(
                "run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "find bugs"},
                MagicMock(return_value=ApprovalDecision(action="approve")),
                False,
            ).ok
            assert registry.execute(
                "run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "find more bugs"},
                MagicMock(return_value=ApprovalDecision(action="approve")),
                False,
            ).ok
            r3 = registry.execute(
                "run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "find even more"},
                MagicMock(return_value=ApprovalDecision(action="approve")),
                False,
            )
            assert r3.ok is False
            assert "limit" in str(r3.payload).lower()


class TestToolCatalogSurface:
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
