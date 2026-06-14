from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura import paths as aura_paths
from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry
from aura.drones.store import DroneStore


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("aura.drones.store.data_dir", lambda: tmp_path / "data")


def _noop_approval(_req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="approve", note="")


def _write_folder(workspace: Path) -> Path:
    folder = workspace / ".aura" / "drone-build" / "test-drone"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(
        "import json, sys\n"
        "def run(payload):\n"
        "    return {'ok': True, 'goal': payload.get('goal')}\n"
        "if __name__ == '__main__':\n"
        "    payload = json.loads(sys.stdin.read())\n"
        "    result = run(payload)\n"
        "    print(json.dumps(result))\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": "test-drone",
                "name": "Test Drone",
                "description": "A folder-backed Drone.",
                "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
                "instructions": "Run the entrypoint.",
                "write_policy": "read_only",
                "allowed_tools": [],
                "output_contract": "Return cargo.",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_register_drone_folder_creates_and_loads_drone(tmp_path: Path) -> None:
    folder = _write_folder(tmp_path)
    registry = ToolRegistry(workspace_root=tmp_path, mode="worker")

    result = registry.execute(
        "register_drone_folder",
        {"folder_path": ".aura/drone-build/test-drone"},
        _noop_approval,
    )

    assert result.ok is True, result.payload
    assert result.extras.get("drone_saved") is True
    assert result.payload["id"] == "test-drone"
    assert isinstance(result.payload["entrypoint"], dict)
    assert result.payload["entrypoint"]["kind"] == "command"

    loaded = DroneStore.load_drone(tmp_path, "test-drone")
    assert loaded is not None
    assert loaded.name == "Test Drone"
    assert loaded.allowed_tools == ()
    assert folder.exists()


def test_register_drone_folder_rejects_broken_entrypoint(tmp_path: Path) -> None:
    folder = _write_folder(tmp_path)
    # Overwrite main.py so it returns invalid JSON (crashes readiness)
    (folder / "main.py").write_text(
        "import sys\n"
        "sys.stderr.write('boom')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(workspace_root=tmp_path, mode="worker")

    result = registry.execute(
        "register_drone_folder",
        {"folder_path": ".aura/drone-build/test-drone"},
        _noop_approval,
    )

    assert result.ok is False
    assert DroneStore.load_drone(tmp_path, "test-drone") is None


def test_removed_definition_tool_is_not_registered(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, mode="worker")
    names = {d["function"]["name"] for d in registry.tool_defs()}
    removed_tool = "save" + "_drone_definition"

    assert "register_drone_folder" in names
    assert removed_tool not in names
    assert removed_tool not in TOOL_HANDLERS

    result = registry.execute(
        removed_tool,
        {"name": "Old Drone"},
        _noop_approval,
    )
    assert result.ok is False
