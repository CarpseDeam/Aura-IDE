from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura import paths as aura_paths
from aura.drones.architect.commands import DroneCommand, parse_drone_command
from aura.drones.architect.controller import DroneArchitectController
from aura.drones.architect.installer import install_or_reinstall
from aura.drones.store import DroneStore
from aura.drones.workspaces.model import WorkspacePhase
from aura.drones.workspaces.paths import candidate_dir
from aura.drones.workspaces.store import DroneWorkspaceStore


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("aura.drones.store.data_dir", lambda: tmp_path / "data")


def _write_drone_folder(folder: Path, *, drone_id: str = "repo-scout") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "print(json.dumps({'ok': True, 'goal': payload.get('goal')}))\n",
        encoding="utf-8",
    )
    (folder / "drone.json").write_text(
        json.dumps(
            {
                "id": drone_id,
                "name": "Repo Scout",
                "description": "Finds repository updates.",
                "instructions": "Inspect repository activity.",
                "write_policy": "read_only",
                "output_contract": "Return repository updates.",
                "entrypoint": {
                    "kind": "command",
                    "command": ["python", "main.py"],
                    "protocol": "json-stdio",
                },
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_drone_enter_mode_creates_visible_draft_entry(tmp_path: Path) -> None:
    controller = DroneArchitectController()
    controller.set_workspace_root(tmp_path)

    result = controller.enter_mode()

    assert result.kind == "workspace_loaded"
    entries = DroneStore.list_drone_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].name == "New Drone"
    assert entries[0].status == "Draft"
    assert entries[0].ready is False
    assert entries[0].workspace_id == result.workspace_id


def test_builder_candidate_entry_is_visible_but_not_runnable(tmp_path: Path) -> None:
    workspace = DroneWorkspaceStore.create_workspace(tmp_path, "Repo Scout")
    _write_drone_folder(candidate_dir(tmp_path, workspace.workspace_id))
    workspace.phase = WorkspacePhase.PROOF_RUNNING.value
    workspace.candidate_drone_id = "repo-scout"
    DroneWorkspaceStore.save_workspace(workspace)

    entries = DroneStore.list_drone_entries(tmp_path)

    assert [(entry.name, entry.status, entry.ready) for entry in entries] == [
        ("Repo Scout", "Testing", False)
    ]
    assert DroneStore.load_drone(tmp_path, "repo-scout") is None


def test_ready_step_promotes_builder_entry_to_ready_drone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    block_real_subprocess,
) -> None:
    monkeypatch.setattr("aura.drones.folder_runner.subprocess.run", block_real_subprocess)
    workspace = DroneWorkspaceStore.create_workspace(tmp_path, "Repo Scout")
    _write_drone_folder(candidate_dir(tmp_path, workspace.workspace_id))

    result = install_or_reinstall(workspace, tmp_path)

    assert result["ok"] is True, result
    entries = DroneStore.list_drone_entries(tmp_path)
    assert [(entry.id, entry.status, entry.ready) for entry in entries] == [
        ("repo-scout", "Ready", True)
    ]
    assert DroneStore.load_drone(tmp_path, "repo-scout") is not None


def test_install_and_register_are_not_user_commands() -> None:
    assert parse_drone_command("register it", "awaiting_decision")[0] is DroneCommand.REVISE
    assert parse_drone_command("register the drone", "awaiting_decision")[0] is DroneCommand.REVISE
    assert parse_drone_command("install", "awaiting_decision")[0] is DroneCommand.REVISE
    assert parse_drone_command("install the drone", "readiness_failed")[0] is DroneCommand.UNKNOWN
