from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from aura.drones.folder_runner import run_folder_drone_sync
from aura.drones.run import DroneRun
from aura.drones.store import DroneStore, RunHistoryStore


@pytest.fixture(autouse=True)
def _patch_drones_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aura.drones.store.aura_root", lambda: tmp_path / "aura_root")


def _register_cancellable_drone(workspace: Path):
    folder = workspace / "build" / "slow-drone"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(
        "import json, sys, time\n"
        "payload = json.loads(sys.stdin.read())\n"
        "time.sleep(30)\n"
        "print(json.dumps({'ok': True, 'finished': True}))\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": "slow-drone",
                "name": "Slow Drone",
                "description": "Sleeps until cancelled.",
                "entrypoint": {
                    "kind": "command",
                    "command": [sys.executable, "main.py"],
                    "protocol": "json-stdio",
                },
                "instructions": "Sleep.",
                "write_policy": "read_only",
                "output_contract": "Return cargo.",
            }
        ),
        encoding="utf-8",
    )
    return DroneStore.register_drone_folder(workspace, folder)


def test_folder_drone_cancel_stops_process_and_receipt_is_cancelled(
    tmp_path: Path,
) -> None:
    drone = _register_cancellable_drone(tmp_path)
    run = DroneRun(drone=drone)
    result_holder: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result",
            run_folder_drone_sync(
                tmp_path,
                drone.id,
                drone,
                "sleep until cancelled",
                run=run,
            ),
        )
    )
    thread.start()

    deadline = time.monotonic() + 5
    while run.status != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    run.cancel()
    thread.join(timeout=5)

    assert not thread.is_alive()
    result = result_holder["result"]
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["status"] == "cancelled"
    assert result["receipt"]["status"] == "cancelled"
    assert run.status == "cancelled"
    saved = RunHistoryStore.load_run(tmp_path, run.run_id)
    assert saved is not None
    assert saved.status == "cancelled"
