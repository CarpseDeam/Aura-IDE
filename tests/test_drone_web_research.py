"""Tests for the bundled Web Research Drone (Phase 3)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aura.drones.store import DroneStore
from aura.drones.sync_runner import run_read_only_drone_sync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_aura_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aura.drones.store.aura_root", lambda: tmp_path / "aura_root")
    monkeypatch.setenv("_AURA_MOCK_WEB_RESEARCH", "1")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def web_research_folder(tmp_path: Path) -> Path:
    bundled = (
        Path(__file__).resolve().parent.parent
        / "aura"
        / "drones"
        / "bundled"
        / "web-research"
    )
    assert bundled.is_dir(), f"Source drone folder not found at {bundled}"

    target = (
        tmp_path
        / "aura_root"
        / "aura"
        / "drones"
        / "bundled"
        / "web-research"
    )
    target.mkdir(parents=True)

    (target / "drone.json").write_text(
        (bundled / "drone.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (target / "main.py").write_text(
        (bundled / "main.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return target


@pytest.fixture
def drone(workspace: Path, web_research_folder: Path) -> DroneStore:
    d = DroneStore.load_drone(workspace, "web-research")
    assert d is not None, "web-research drone should be loadable from the store"
    return d


# ---------------------------------------------------------------------------
# Store / manifest tests
# ---------------------------------------------------------------------------


class TestStoreLoading:
    def test_web_research_loads_from_store(self, workspace: Path, drone: DroneStore) -> None:
        assert drone.id == "web-research"
        assert drone.kind == "command"
        assert drone.write_policy == "read_only"
        assert drone.entrypoint["protocol"] == "json-stdio"

    def test_web_research_appears_in_list_drones(
        self, workspace: Path, web_research_folder: Path
    ) -> None:
        ids = [d.id for d in DroneStore.list_drones(workspace)]
        assert "web-research" in ids

    def test_manifest_validates_as_command_read_only_drone(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        assert drone.kind == "command"
        assert drone.write_policy == "read_only"
        assert drone.entrypoint["protocol"] == "json-stdio"
        assert isinstance(drone.output_contract, dict)
        assert drone.budget.timeout_seconds == 120


# ---------------------------------------------------------------------------
# Execution tests  (run through the real runner)
# ---------------------------------------------------------------------------


class TestRunWithRunner:
    def test_run_with_query_line_goal(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        result = run_read_only_drone_sync(
            workspace,
            "web-research",
            drone,
            "query: World Cup matches today\nfreshness: today",
        )
        assert result["ok"] is True
        assert result["status"] == "completed"
        assert "Completed live web research" in result["summary"]

    def test_plain_goal_text_as_query(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        goal = "What is the latest Python version?"
        result = run_read_only_drone_sync(workspace, "web-research", drone, goal)
        assert result["ok"] is True
        assert result["status"] == "completed"
        assert "Completed live web research" in result["summary"]

    def test_empty_goal_returns_failure(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        result = run_read_only_drone_sync(workspace, "web-research", drone, "")
        assert result["ok"] is False
        assert result["status"] == "failed"
        assert "no query" in result["summary"].lower()

    def test_whitespace_only_goal_returns_failure(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        result = run_read_only_drone_sync(
            workspace, "web-research", drone, "   \t  \n  "
        )
        assert result["ok"] is False
        assert result["status"] == "failed"
        assert "no query" in result["summary"].lower()


# ---------------------------------------------------------------------------
# Output shape  (run the drone directly to capture full stdout JSON)
# ---------------------------------------------------------------------------


class TestOutputShape:
    _PAYLOAD = {
        "goal": "What is the capital of France?",
        "workspace_root": "/tmp",
        "drone_id": "web-research",
        "input": {},
        "upstream": {},
    }

    def _run_drone(
        self, folder: Path, goal: str, monkeypatch, block_real_subprocess
    ) -> dict:
        payload = {**self._PAYLOAD, "goal": goal}
        monkeypatch.setattr(subprocess, "run", block_real_subprocess)
        monkeypatch.setenv("_AURA_MOCK_WEB_RESEARCH", "1")
        proc = subprocess.run(
            [sys.executable, "main.py"],
            cwd=folder,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        return json.loads(proc.stdout.strip())

    def test_returned_cargo_shape(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "What is the capital of France?",
            monkeypatch, block_real_subprocess,
        )

        required_keys = {
            "ok", "summary", "query",
            "verified_facts", "sources", "evidence", "gaps",
            "confidence", "trace", "route_used",
        }
        assert required_keys.issubset(data.keys()), (
            f"Missing keys: {required_keys - data.keys()}"
        )

        assert isinstance(data["verified_facts"], list)
        assert isinstance(data["gaps"], list)
        assert isinstance(data["trace"], list)
        assert isinstance(data["route_used"], dict)

        assert data["ok"] is True
        assert data["confidence"] == "medium"
        assert data["query"] == "What is the capital of France?"
        assert "live_research_ready" not in data

    def test_empty_goal_direct_output(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "", monkeypatch, block_real_subprocess,
        )
        assert data["ok"] is False
        assert "query is required" in data["error"]
        assert "no query" in data["summary"].lower()

    def test_mocked_world_cup_schedule_result(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "What time are World Cup matches today?",
            monkeypatch, block_real_subprocess,
        )
        assert data["ok"] is True
        assert data["confidence"] != "none"
        assert any("World Cup Matches Today: USA vs ENG" in ev["excerpt"] for ev in data["evidence"])
        assert any("fifa.com" in src["url"] for src in data["sources"])

    def test_mocked_no_evidence_result(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "fail query",
            monkeypatch, block_real_subprocess,
        )
        assert data["ok"] is True
        assert data["confidence"] in ["none", "low"]
        assert len(data["gaps"]) > 0
        assert "HTTP fetch error" in data["gaps"][0]
