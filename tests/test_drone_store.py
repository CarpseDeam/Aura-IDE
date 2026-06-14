from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura import paths as aura_paths
from aura.drones.definition import DroneDefinition, slugify
from aura.drones.store import DroneStore, _drone_from_dict, _global_drones_root


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("aura.drones.store.data_dir", lambda: tmp_path / "data")


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
                "allowed_tools": ["read_file"],
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_list_drones_empty(tmp_path: Path) -> None:
    assert DroneStore.list_drones(tmp_path) == []
    assert not _global_drones_root().exists()


def test_register_load_and_list_folder_drone(tmp_path: Path) -> None:
    source = _write_drone_folder(tmp_path / "build")

    drone = DroneStore.register_drone_folder(tmp_path, source)

    assert drone.id == "folder-drone"
    assert drone.entrypoint == {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"}
    assert drone.allowed_tools == ("read_file",)
    loaded = DroneStore.load_drone(tmp_path, "folder-drone")
    assert loaded == drone
    assert [d.id for d in DroneStore.list_drones(tmp_path)] == ["folder-drone"]


def test_allowed_tools_defaults_empty_and_does_not_make_valid_drone() -> None:
    prompt_only = {
        "id": "prompt-only",
        "name": "Prompt Only",
        "description": "Old shape.",
        "instructions": "Use tools.",
        "write_policy": "read_only",
        "output_contract": "Summary.",
        "allowed_tools": ["read_file"],
    }

    with pytest.raises(ValueError, match="entrypoint is required"):
        _drone_from_dict(prompt_only)

    valid = {
        **prompt_only,
        "entrypoint": {"kind": "command", "command": ["python", "main.py"], "protocol": "json-stdio"},
    }
    drone = _drone_from_dict(valid)
    assert drone.allowed_tools == ("read_file",)

    valid_without_tools = {k: v for k, v in valid.items() if k != "allowed_tools"}
    assert _drone_from_dict(valid_without_tools).allowed_tools == ()


def test_register_requires_entrypoint_module(tmp_path: Path) -> None:
    source = _write_drone_folder(tmp_path / "build")
    (source / "main.py").unlink()

    with pytest.raises(ValueError, match="readiness"):
        DroneStore.register_drone_folder(tmp_path, source)


def test_register_rejects_broken_readiness(tmp_path: Path) -> None:
    source = _write_drone_folder(tmp_path / "build")
    # Corrupt main.py so readiness fails
    (source / "main.py").write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "result = {'ok': False, 'error': 'not ready'}\n"
        "print(json.dumps(result))\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="readiness"):
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
                "allowed_tools": ["read_file"],
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
            "allowed_tools": (),
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
