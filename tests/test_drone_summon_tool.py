from __future__ import annotations

import json

import pytest

from aura import paths as aura_paths
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.drones.store import DroneStore


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("aura.drones.store.data_dir", lambda: tmp_path / "data")


def _register_drone(tmp_path) -> None:
    folder = tmp_path / "build" / "test-scout"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(
        "import json, sys\n"
        "def run(payload):\n"
        "    return {'ok': True}\n"
        "if __name__ == '__main__':\n"
        "    payload = json.loads(sys.stdin.read())\n"
        "    result = run(payload)\n"
        "    print(json.dumps(result))\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": "test-scout",
                "name": "Test Scout",
                "description": "Find tests.",
                "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
                "instructions": "Find relevant tests for the current change.",
                "write_policy": "read_only",
                "output_contract": "Test list and rationale.",
            }
        ),
        encoding="utf-8",
    )
    DroneStore.register_drone_folder(tmp_path, folder)


def test_summon_drone_returns_confirmation_metadata(tmp_path) -> None:
    _register_drone(tmp_path)
    registry = ToolRegistry(tmp_path, mode="planner")

    result = registry.execute(
        "summon_drone",
        {"drone_id": "test-scout", "goal": "Find the right tests"},
        lambda request: ApprovalDecision("approve"),
    )

    assert result.ok is True
    assert result.payload["status"] == "pending_user_confirmation"
    assert result.payload["drone_name"] == "Test Scout"
    assert result.extras["summon_drone"] is True


def test_summon_drone_rejects_unknown_drone(tmp_path) -> None:
    registry = ToolRegistry(tmp_path, mode="planner")

    result = registry.execute(
        "summon_drone",
        {"drone_id": "missing", "goal": "Find tests"},
        lambda request: ApprovalDecision("approve"),
    )

    assert result.ok is False
    assert "unknown drone" in result.payload["error"]
