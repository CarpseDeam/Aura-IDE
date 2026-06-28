from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry
from aura.drones.store import DroneStore


@pytest.fixture(autouse=True)
def _patch_drones_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aura.drones.store.aura_root", lambda: tmp_path / "aura_root")


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
        "import json, sys\n"
        "def run(payload):\n"
        "    return {'ok': True, 'summary': payload.get('goal')}\n"
        "if __name__ == '__main__':\n"
        "    payload = json.loads(sys.stdin.read())\n"
        "    result = run(payload)\n"
        "    print(json.dumps(result))\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": drone_id,
                "name": "Bug Scout" if write_policy == "read_only" else "Writer Drone",
                "description": "Investigates bugs.",
                "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
                "instructions": "Run the entrypoint.",
                "write_policy": write_policy,
                "output_contract": "Return cargo.",
            }
        ),
        encoding="utf-8",
    )
    DroneStore.register_drone_folder(workspace, folder)
    return drone_id


def _register_web_research_drone(workspace: Path) -> str:
    folder = workspace / "build" / "web-research"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'ok': True, 'summary': payload.get('goal')}))\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": "web-research",
                "name": "Web Research",
                "description": "Researches current-info questions.",
                "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
                "instructions": "Run the entrypoint.",
                "write_policy": "read_only",
                "output_contract": "Return sourced research.",
            }
        ),
        encoding="utf-8",
    )
    DroneStore.register_drone_folder(workspace, folder)
    return "web-research"


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

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_web_research_dispatch_payload_includes_user_question(
        self, mock_runner, workspace: Path
    ):
        _register_web_research_drone(workspace)
        question = "What time are World Cup matches today?"
        mock_runner.return_value = {
            "ok": True,
            "run_id": "run-web",
            "drone_id": "web-research",
            "drone_name": "Web Research",
            "status": "completed",
            "summary": "researched",
            "cargo": {
                "answer": "USA vs ENG is at 8:00 PM local time.",
                "verified_facts": ["USA vs ENG is listed at 8:00 PM."],
                "sources": [{"title": "FIFA Match Centre", "url": "https://www.fifa.com/en/match-centre"}],
                "evidence": [{"excerpt": "USA vs ENG 8:00 PM"}],
                "gaps": [],
                "confidence": "medium",
            },
        }
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")

        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "web-research", "goal": question},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )

        assert result.ok is True
        mock_runner.assert_called_once()
        assert mock_runner.call_args.kwargs["drone_id"] == "web-research"
        assert mock_runner.call_args.kwargs["goal"] == question

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_web_research_success_adds_sourced_chat_answer(
        self, mock_runner, workspace: Path
    ):
        _register_web_research_drone(workspace)
        mock_runner.return_value = {
            "ok": True,
            "run_id": "run-web",
            "drone_id": "web-research",
            "drone_name": "Web Research",
            "status": "completed",
            "summary": "researched",
            "cargo": {
                "answer": "USA vs ENG is at 8:00 PM local time.",
                "verified_facts": ["USA vs ENG is listed at 8:00 PM."],
                "sources": [{"title": "FIFA Match Centre", "url": "https://www.fifa.com/en/match-centre"}],
                "evidence": [{"excerpt": "USA vs ENG 8:00 PM"}],
                "gaps": [],
                "confidence": "medium",
            },
        }
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")

        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "web-research", "goal": "What time are World Cup matches today?"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )

        answer = result.payload["answer_for_chat"]
        assert "8:00 PM" in answer
        assert "FIFA Match Centre" in answer
        assert "https://www.fifa.com/en/match-centre" in answer

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_web_research_direct_handler_produces_answer_for_chat_without_name_error(
        self, mock_runner, workspace: Path
    ):
        _register_web_research_drone(workspace)
        mock_runner.return_value = {
            "ok": True,
            "run_id": "run-web",
            "drone_id": "web-research",
            "drone_name": "Web Research",
            "status": "completed",
            "summary": "researched",
            "cargo": {
                "answer": "Python 3.14.0 is the latest stable version.",
                "verified_facts": ["Python 3.14.0 is listed as stable."],
                "sources": [{"title": "Python Downloads", "url": "https://www.python.org/downloads/"}],
                "evidence": [{"excerpt": "Python 3.14.0"}],
                "gaps": [],
                "confidence": "high",
            },
        }
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")

        result = registry._handle_run_read_only_drone(
            {"drone_id": "web-research", "goal": "What is the latest Python version?"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )

        assert result.ok is True
        assert result.payload["answer_for_chat"]
        assert "Python 3.14.0" in result.payload["answer_for_chat"]
        assert "Python Downloads" in result.payload["answer_for_chat"]

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_web_research_low_confidence_adds_careful_chat_answer(
        self, mock_runner, workspace: Path
    ):
        _register_web_research_drone(workspace)
        mock_runner.return_value = {
            "ok": True,
            "run_id": "run-web",
            "drone_id": "web-research",
            "drone_name": "Web Research",
            "status": "completed",
            "summary": "not enough evidence",
            "cargo": {
                "answer": "",
                "verified_facts": [],
                "sources": [],
                "evidence": [],
                "gaps": ["No official schedule source was reachable."],
                "confidence": "none",
            },
        }
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")

        result = registry.execute(
            "run_read_only_drone",
            {"drone_id": "web-research", "goal": "What time are World Cup matches today?"},
            MagicMock(return_value=ApprovalDecision(action="approve")),
            False,
        )

        answer = result.payload["answer_for_chat"]
        assert "could not verify" in answer.lower()
        assert "No official schedule source was reachable" in answer

    def test_per_turn_limit_allows_exactly_3_and_blocks_4th(self, workspace: Path):
        _register_drone(workspace)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        with patch("aura.drones.sync_runner.run_read_only_drone_sync") as mock_runner:
            mock_runner.return_value = {
                "ok": True, "run_id": "r", "drone_id": "bug-scout",
                "drone_name": "Bug Scout", "status": "completed",
                "summary": "ok", "tool_calls_made": 0,
                "tool_errors": 0, "elapsed_seconds": 0.1,
            }
            # Run 1, 2, 3 — all OK
            assert registry.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g1"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False).ok
            assert registry.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g2"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False).ok
            assert registry.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g3"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False).ok
            # Run 4 — blocked
            r4 = registry.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g4"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False)
            assert r4.ok is False
            assert r4.payload.get("code") == "drone_budget_exhausted"

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_web_research_allows_6_and_blocks_7th(self, mock_runner, workspace: Path):
        _register_web_research_drone(workspace)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        mock_runner.return_value = {
            "ok": True, "run_id": "r", "drone_id": "web-research",
            "drone_name": "Web Research", "status": "completed",
            "summary": "ok", "tool_calls_made": 0,
            "tool_errors": 0, "elapsed_seconds": 0.1,
        }
        # Runs 1–6 OK
        for i in range(6):
            assert registry.execute("run_read_only_drone",
                {"drone_id": "web-research", "goal": f"g{i}"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False).ok
        # Run 7 blocked
        r7 = registry.execute("run_read_only_drone",
            {"drone_id": "web-research", "goal": "g7"},
            MagicMock(return_value=ApprovalDecision(action="approve")), False)
        assert r7.ok is False
        assert r7.payload.get("code") == "drone_budget_exhausted"
        assert r7.payload.get("limit") == 6

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_reset_drone_budget_allows_new_runs(self, mock_runner, workspace: Path):
        _register_drone(workspace)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        mock_runner.return_value = {
            "ok": True, "run_id": "r", "drone_id": "bug-scout",
            "drone_name": "Bug Scout", "status": "completed",
            "summary": "ok", "tool_calls_made": 0,
            "tool_errors": 0, "elapsed_seconds": 0.1,
        }
        # Exhaust budget
        for _ in range(3):
            registry.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False)
        assert registry.execute("run_read_only_drone",
            {"drone_id": "bug-scout", "goal": "g4"},
            MagicMock(return_value=ApprovalDecision(action="approve")), False).ok is False
        # Reset and try again
        registry.reset_drone_budget()
        assert registry.execute("run_read_only_drone",
            {"drone_id": "bug-scout", "goal": "g5"},
            MagicMock(return_value=ApprovalDecision(action="approve")), False).ok

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_fresh_chat_does_not_inherit_budget(self, mock_runner, workspace: Path):
        """Two registries should have independent budgets."""
        _register_drone(workspace)
        registry1 = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        registry2 = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        mock_runner.return_value = {
            "ok": True, "run_id": "r", "drone_id": "bug-scout",
            "drone_name": "Bug Scout", "status": "completed",
            "summary": "ok", "tool_calls_made": 0,
            "tool_errors": 0, "elapsed_seconds": 0.1,
        }
        # Exhaust registry1
        for _ in range(3):
            registry1.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False)
        # registry2 should still have full budget
        for _ in range(3):
            assert registry2.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False).ok
        assert registry2.execute("run_read_only_drone",
            {"drone_id": "bug-scout", "goal": "g4"},
            MagicMock(return_value=ApprovalDecision(action="approve")), False).ok is False

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_exhausted_budget_structured_error(self, mock_runner, workspace: Path):
        _register_drone(workspace)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        mock_runner.return_value = {
            "ok": True, "run_id": "r", "drone_id": "bug-scout",
            "drone_name": "Bug Scout", "status": "completed",
            "summary": "ok", "tool_calls_made": 0,
            "tool_errors": 0, "elapsed_seconds": 0.1,
        }
        # Run 3 successful, then check structured error on 4th
        for _ in range(3):
            registry.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False)
        result = registry.execute("run_read_only_drone",
            {"drone_id": "bug-scout", "goal": "g4"},
            MagicMock(return_value=ApprovalDecision(action="approve")), False)
        assert result.ok is False
        payload = result.payload
        assert payload.get("code") == "drone_budget_exhausted"
        assert payload.get("limit") == 3
        assert payload.get("used") == 3
        assert payload.get("drone_id") == "bug-scout"
        assert "limit" in payload.get("error", "").lower()

    @patch("aura.drones.sync_runner.run_read_only_drone_sync")
    def test_exhausted_budget_clean_error_has_no_internal_self_talk(self, mock_runner, workspace: Path):
        """Ensure exhausted-budget error payload does not contain internal self-talk phrases."""
        _register_drone(workspace)
        registry = ToolRegistry(workspace_root=workspace, read_only=False, mode="planner")
        mock_runner.return_value = {
            "ok": True, "run_id": "r", "drone_id": "bug-scout",
            "drone_name": "Bug Scout", "status": "completed",
            "summary": "ok", "tool_calls_made": 0,
            "tool_errors": 0, "elapsed_seconds": 0.1,
        }
        for _ in range(3):
            registry.execute("run_read_only_drone",
                {"drone_id": "bug-scout", "goal": "g"},
                MagicMock(return_value=ApprovalDecision(action="approve")), False)
        result = registry.execute("run_read_only_drone",
            {"drone_id": "bug-scout", "goal": "g4"},
            MagicMock(return_value=ApprovalDecision(action="approve")), False)
        error_text = str(result.payload).lower()
        # None of these internal self-talk phrases should appear
        forbidden = ["actually", "maybe i should", "let me be honest",
                     "i don't have real-time data access in this environment",
                     "start a fresh turn"]
        for phrase in forbidden:
            assert phrase not in error_text, f"Forbidden phrase found: '{phrase}'"



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
