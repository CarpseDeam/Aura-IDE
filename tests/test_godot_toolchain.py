from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from aura.conversation.project_profile import detect_project_profile
from aura.godot_toolchain import (
    GODOT_SETUP_MESSAGE,
    build_godot_check_command,
    build_godot_import_command,
    build_godot_resource_check_command,
    filesystem_path_to_res_path,
    find_godot_project_root,
    load_godot_executable_setting,
    resolve_godot_executable,
    save_godot_executable_setting,
)
from aura.validation.selector import select_validation_plan


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.godot").write_text("[application]\n", encoding="utf-8")
    return root


def test_detects_project_godot_at_workspace_and_ancestor(tmp_path: Path) -> None:
    project_root = _project(tmp_path / "game")
    nested_workspace = project_root / "addons" / "tool"
    nested_workspace.mkdir(parents=True)

    assert find_godot_project_root(project_root) == project_root.resolve()
    assert find_godot_project_root(nested_workspace) == project_root.resolve()

    profile = detect_project_profile(nested_workspace)
    assert "godot" in profile.project_types
    assert "project.godot" in profile.manifests
    assert profile.godot_project_root == str(project_root.resolve())


def test_profile_shows_focused_validation_format(tmp_path: Path) -> None:
    project_root = _project(tmp_path / "game")
    executable = _executable(tmp_path / "Godot Tools" / "Godot_v4.6.exe")
    metadata = project_root / ".aura" / "project.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps({"godot_executable": str(executable)}),
        encoding="utf-8",
    )

    summary = detect_project_profile(project_root).summarize()

    assert "Godot focused validation format:" in summary
    assert "--check-only" in summary
    assert "--script" in summary
    assert "res://path/to/touched_file.gd" in summary
    assert "Godot project import validation:" in summary
    assert "--import" in summary
    assert "Aura live Godot editor bridge: bundled" in summary
    assert "do not author a replacement plugin" in summary
    assert "install_godot_editor_bridge" in summary


def test_configured_executable_path_wins(tmp_path: Path) -> None:
    root = _project(tmp_path / "game")
    configured = _executable(tmp_path / "Desktop" / "Godot configured.exe")
    env_candidate = _executable(tmp_path / "Godot env.exe")
    metadata = root / ".aura" / "project.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps({"godot_executable": str(configured)}),
        encoding="utf-8",
    )

    result = resolve_godot_executable(
        root,
        environ={"GODOT_BIN": str(env_candidate)},
        which=lambda _name: str(env_candidate),
    )

    assert result.path == configured.resolve()
    assert result.source == "godot_executable"


def test_project_setting_round_trip_preserves_project_metadata(tmp_path: Path) -> None:
    root = _project(tmp_path / "game")
    metadata = root / ".aura" / "project.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps({"id": "project-1", "name": "Game", "root_path": str(root)}),
        encoding="utf-8",
    )

    save_godot_executable_setting(root, r"C:\Users\Kori\Desktop\Godot_v4.6-stable_win64.exe")

    saved = json.loads(metadata.read_text(encoding="utf-8"))
    assert saved["id"] == "project-1"
    assert load_godot_executable_setting(root) == (
        r"C:\Users\Kori\Desktop\Godot_v4.6-stable_win64.exe"
    )


def test_godot_bin_fallback(tmp_path: Path) -> None:
    executable = _executable(tmp_path / "Godot env.exe")

    result = resolve_godot_executable(
        tmp_path,
        environ={"GODOT_BIN": str(executable)},
        which=lambda _name: None,
    )

    assert result.path == executable.resolve()
    assert result.source == "GODOT_BIN"


def test_path_fallback_is_mockable(tmp_path: Path) -> None:
    executable = _executable(tmp_path / "bin" / "godot4.exe")
    requested: list[str] = []

    def fake_which(name: str) -> str | None:
        requested.append(name)
        return str(executable) if name == "godot4" else None

    result = resolve_godot_executable(tmp_path, environ={}, which=fake_which)

    assert result.path == executable.resolve()
    assert result.source == "PATH"
    assert requested[:3] == ["godot", "godot.exe", "godot4"]


def test_windows_desktop_candidate_discovery_is_mockable(tmp_path: Path) -> None:
    executable = tmp_path / "Desktop" / "Godot_v4.6-stable_win64.exe"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")

    result = resolve_godot_executable(
        tmp_path,
        environ={},
        which=lambda _name: None,
        home=tmp_path,
        platform="nt",
    )

    assert result.path == executable.resolve()
    assert result.source == "Desktop"


def test_windows_redirected_onedrive_desktop_candidate_is_discovered(tmp_path: Path) -> None:
    one_drive = tmp_path / "OneDrive - Example"
    executable = one_drive / "Desktop" / "Godot_v4.6.3-stable_win64.exe"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")

    result = resolve_godot_executable(
        tmp_path,
        environ={"OneDrive": str(one_drive)},
        which=lambda _name: None,
        home=tmp_path / "local-home",
        platform="nt",
    )

    assert result.path == executable.resolve()
    assert result.source == "Desktop"


def test_missing_godot_is_non_fatal_unavailable(tmp_path: Path) -> None:
    result = resolve_godot_executable(
        tmp_path,
        environ={},
        which=lambda _name: None,
        platform="posix",
    )

    assert not result.available
    assert result.path is None
    assert result.message == GODOT_SETUP_MESSAGE


def test_filesystem_path_converts_to_res_uri(tmp_path: Path) -> None:
    root = _project(tmp_path / "game")
    script = root / "scripts" / "ruin_catalog.gd"

    assert filesystem_path_to_res_path(script, root) == "res://scripts/ruin_catalog.gd"

    with pytest.raises(ValueError, match="outside Godot project root"):
        filesystem_path_to_res_path(tmp_path / "other.gd", root)


def test_validation_commands_quote_paths_safely(tmp_path: Path) -> None:
    root = _project(tmp_path / "Game With Spaces")
    executable = _executable(tmp_path / "Godot Tools" / "godot.exe")
    script = root / "scripts" / "ruin catalog.gd"

    check_command = build_godot_check_command(executable, root, script, platform="posix")
    import_command = build_godot_import_command(executable, root, platform="posix")

    assert check_command is not None
    assert shlex.split(check_command) == [
        str(executable),
        "--headless",
        "--path",
        str(root.resolve()),
        "--check-only",
        "--script",
        "res://scripts/ruin catalog.gd",
    ]
    assert shlex.split(import_command) == [
        str(executable),
        "--headless",
        "--path",
        str(root.resolve()),
        "--import",
    ]


def test_windows_validation_command_preserves_spaced_paths(tmp_path: Path) -> None:
    root = _project(tmp_path / "Game With Spaces")
    executable = _executable(tmp_path / "Godot Tools" / "Godot_v4.6-stable_win64.exe")
    script = root / "scripts" / "ruin catalog.gd"

    command = build_godot_check_command(executable, root, script, platform="nt")

    assert command is not None
    assert command.startswith(f'"{executable}" --headless')
    assert f'--path "{root.resolve()}"' in command
    assert '--script "res://scripts/ruin catalog.gd"' in command


def test_non_gd_file_does_not_produce_check_command(tmp_path: Path) -> None:
    root = _project(tmp_path / "game")
    executable = _executable(tmp_path / "godot.exe")

    assert build_godot_check_command(executable, root, root / "icon.svg") is None


def test_resource_validation_command_loads_touched_scenes(tmp_path: Path) -> None:
    root = _project(tmp_path / "Game With Spaces")
    executable = _executable(tmp_path / "Godot Tools" / "godot.exe")
    validator = tmp_path / "Aura Tools" / "validate.gd"
    validator.parent.mkdir()
    validator.write_text("extends SceneTree\n", encoding="utf-8")

    command = build_godot_resource_check_command(
        executable,
        root,
        [root / "scenes" / "player.tscn", root / "data" / "stats.tres"],
        platform="posix",
        validator_script=validator,
    )

    assert command is not None
    assert shlex.split(command) == [
        str(executable),
        "--headless",
        "--path",
        str(root.resolve()),
        "--script",
        str(validator.resolve()),
        "--",
        "res://scenes/player.tscn",
        "res://data/stats.tres",
    ]


def test_selector_uses_real_godot_for_touched_gd_files(tmp_path: Path) -> None:
    root = _project(tmp_path / "game")
    executable = _executable(tmp_path / "Godot Tools" / "godot.exe")
    metadata = root / ".aura" / "project.json"
    metadata.parent.mkdir()
    metadata.write_text(json.dumps({"godot_executable": str(executable)}), encoding="utf-8")

    plan = select_validation_plan(
        target_files=["scripts/ruin_catalog.gd", "icon.svg"],
        changed_files=["scripts/ruin_catalog.gd", "icon.svg"],
        workspace_root=root,
    )

    assert plan["kind"] == "godot"
    assert plan["available"] is True
    assert len(plan["commands"]) == 1
    assert "--check-only" in plan["commands"][0]
    assert "res://scripts/ruin_catalog.gd" in plan["commands"][0]
    assert "icon.svg" not in plan["commands"][0]


def test_selector_reports_missing_godot_without_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aura.godot_toolchain as godot_toolchain

    root = _project(tmp_path / "game")
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))
    monkeypatch.setattr(godot_toolchain, "_windows_registered_desktop", lambda _env: None)

    plan = select_validation_plan(
        target_files=["player.gd"],
        changed_files=["player.gd"],
        workspace_root=root,
    )

    assert plan["kind"] == "godot"
    assert plan["available"] is False
    assert plan["commands"] == []
    assert plan["setup_message"] == GODOT_SETUP_MESSAGE


def test_selector_imports_and_loads_touched_scene(tmp_path: Path) -> None:
    root = _project(tmp_path / "game")
    executable = _executable(tmp_path / "Godot Tools" / "godot.exe")
    metadata = root / ".aura" / "project.json"
    metadata.parent.mkdir()
    metadata.write_text(json.dumps({"godot_executable": str(executable)}), encoding="utf-8")

    plan = select_validation_plan(
        target_files=["scenes/player.tscn"],
        changed_files=["scenes/player.tscn"],
        workspace_root=root,
    )

    assert plan["kind"] == "godot"
    assert plan["available"] is True
    assert len(plan["commands"]) == 2
    assert "--import" in plan["commands"][0]
    assert "godot_resource_validator.gd" in plan["commands"][1]
    assert "res://scenes/player.tscn" in plan["commands"][1]
