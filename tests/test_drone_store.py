from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura import paths as aura_paths
from aura.drones.definition import DroneDefinition, slugify
from aura.drones.store import DroneStore, _drone_from_dict, _global_drones_root


@pytest.fixture(autouse=True)
def _patch_drones_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_paths, "aura_root", lambda: tmp_path / "aura_root")


def _write_drone_folder(
    root: Path,
    *,
    drone_id: str = "folder-drone",
) -> Path:
    folder = root / drone_id
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(
        "import json, sys\n"
        "def run(payload):\n"
        "    return {\n"
        "        'ok': True,\n"
        "        'goal': payload.get('goal'),\n"
        "        'workspace_root': payload.get('workspace_root'),\n"
        "        'drone_id': payload.get('drone_id'),\n"
        "    }\n"
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
                "name": "Folder Drone",
                "description": "Runs from a folder.",
                "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
                "instructions": "Run the folder entrypoint.",
                "write_policy": "read_only",
                "output_contract": "Return cargo.",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_list_drones_empty(tmp_path: Path) -> None:
    assert DroneStore.list_drones(tmp_path) == []
    assert not _global_drones_root(tmp_path).exists()


def test_register_load_and_list_folder_drone(tmp_path: Path) -> None:
    source = _write_drone_folder(tmp_path / "build")

    drone = DroneStore.register_drone_folder(tmp_path, source)

    assert drone.id == "folder-drone"
    assert drone.entrypoint == {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"}
    loaded = DroneStore.load_drone(tmp_path, "folder-drone")
    assert loaded == drone
    assert [d.id for d in DroneStore.list_drones(tmp_path)] == ["folder-drone"]


def test_unknown_fields_dropped_by_drone_from_dict() -> None:
    """Unknown fields in JSON dict are silently dropped by _drone_from_dict."""
    prompt_only = {
        "id": "prompt-only",
        "name": "Prompt Only",
        "description": "Old shape.",
        "instructions": "Use tools.",
        "write_policy": "read_only",
        "output_contract": "Summary.",
        "unknown_field": "should be dropped",
    }

    with pytest.raises(ValueError, match="entrypoint is required"):
        _drone_from_dict(prompt_only)

    valid = {
        **prompt_only,
        "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
    }
    drone = _drone_from_dict(valid)
    assert drone.id == "prompt-only"
    assert drone.entrypoint["command"] == ["python", "main.py"]


def test_register_requires_entrypoint_module(tmp_path: Path) -> None:
    source = _write_drone_folder(tmp_path / "build")
    # Use ./ prefix so validation checks the file exists in the folder
    drone_json_path = source / "drone.json"
    data = json.loads(drone_json_path.read_text(encoding="utf-8"))
    data["entrypoint"]["command"] = ["./main.py"]
    drone_json_path.write_text(json.dumps(data), encoding="utf-8")
    (source / "main.py").unlink()

    with pytest.raises(ValueError, match="not found"):
        DroneStore.register_drone_folder(tmp_path, source)


def test_workspace_json_drones_are_not_loaded(tmp_path: Path) -> None:
    workspace_dir = tmp_path / ".aura" / "drones"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "old.json").write_text(
        json.dumps(
            {
                "id": "old",
                "name": "Old",
                "description": "Old manifest-only Drone.",
                "instructions": "Use tools.",
                "write_policy": "read_only",
                "output_contract": "Summary.",
            }
        ),
        encoding="utf-8",
    )

    assert DroneStore.list_drones(tmp_path) == []
    assert DroneStore.load_drone(tmp_path, "old") is None


def test_save_drone_updates_registered_manifest_only(tmp_path: Path) -> None:
    source = _write_drone_folder(tmp_path / "build")
    drone = DroneStore.register_drone_folder(tmp_path, source)

    updated = DroneDefinition(
        **{
            **drone.__dict__,
            "name": "Updated Folder Drone",
        }
    )
    DroneStore.save_drone(tmp_path, updated)

    assert DroneStore.load_drone(tmp_path, "folder-drone").name == "Updated Folder Drone"  # type: ignore[union-attr]


def test_save_drone_does_not_create_manifest_only_drone(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="new",
        name="New",
        description="New",
        instructions="Run",
        write_policy="read_only",
        output_contract="Output",
        entrypoint={"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
    )

    with pytest.raises(ValueError, match="register_drone_folder"):
        DroneStore.save_drone(tmp_path, drone)


def test_next_id_only_checks_global_registered_folders(tmp_path: Path) -> None:
    assert DroneStore.next_id(tmp_path, "Release Check") == "release-check"
    DroneStore.register_drone_folder(
        tmp_path,
        _write_drone_folder(tmp_path / "build", drone_id="release-check"),
    )
    assert DroneStore.next_id(tmp_path, "Release Check") == "release-check-1"


def test_slugify() -> None:
    assert slugify("Release Check") == "release-check"
    assert slugify("Hello World!") == "hello-world"
    assert slugify("---") == ""


def test_list_drone_entries_ready_appears_once(tmp_path: Path) -> None:
    """A registered drone shows exactly one entry with status Ready."""
    source = _write_drone_folder(tmp_path / "build", drone_id="ready-test")
    DroneStore.register_drone_folder(tmp_path, source)

    entries = DroneStore.list_drone_entries(tmp_path)
    ready = [e for e in entries if e.status == "Ready" and e.ready]
    assert len(ready) == 1
    assert ready[0].id == "ready-test"
    assert ready[0].name == "Folder Drone"



