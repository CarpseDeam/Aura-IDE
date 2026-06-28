from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.runner import DroneRunner
from aura.drones.store import DroneStore


@pytest.fixture(autouse=True)
def _patch_drones_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aura.drones.store.aura_root", lambda: tmp_path / "aura_root")


def _register_folder_drone(workspace: Path) -> DroneDefinition:
    folder = workspace / "build" / "runner-drone"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(
        "import json, sys\n"
        "def run(payload):\n"
        "    return {'ok': True, 'goal': payload.get('goal'), 'message': 'ran'}\n"
        "if __name__ == '__main__':\n"
        "    payload = json.loads(sys.stdin.read())\n"
        "    result = run(payload)\n"
        "    print(json.dumps(result))\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": "runner-drone",
                "name": "Runner Drone",
                "description": "Run this goal.",
                "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
                "instructions": "Run.",
                "write_policy": "read_only",
                "output_contract": "Return cargo.",
            }
        ),
        encoding="utf-8",
    )
    return DroneStore.register_drone_folder(workspace, folder)


def test_drone_runner_executes_folder_entrypoint(tmp_path: Path) -> None:
    drone = _register_folder_drone(tmp_path)
    runner = DroneRunner(tmp_path, drone)
    statuses: list[str] = []
    chunks: list[str] = []
    receipts: list[DroneReceipt] = []

    runner.statusChanged.connect(statuses.append)
    runner.contentDelta.connect(chunks.append)
    runner.receiptReady.connect(receipts.append)

    runner.run()

    assert statuses[-1] == "completed"
    assert "".join(chunks) == (
        '{\n'
        '  "ok": true,\n'
        '  "goal": "Run this goal.",\n'
        '  "message": "ran"\n'
        '}'
    )
    assert len(receipts) == 1
    assert receipts[0].status == "completed"
    assert receipts[0].produced_artifact == {"ok": True, "goal": "Run this goal.", "message": "ran"}


def test_drone_runner_rejects_non_folder_drone(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="old",
        name="Old",
        description="Old",
        instructions="Use tools.",
        write_policy="read_only",
        output_contract={"description": "Summary.", "properties": {"ok": {"type": "boolean"}, "summary": {"type": "string"}}, "required": ["ok", "summary"]},
    )
    runner = DroneRunner(tmp_path, drone)
    errors: list[tuple[int, str]] = []
    receipts: list[DroneReceipt] = []

    runner.apiError.connect(lambda code, message: errors.append((code, message)))
    runner.receiptReady.connect(receipts.append)

    runner.run()

    assert errors
    assert "folder-backed" in errors[0][1]
    assert receipts[0].status == "failed"
