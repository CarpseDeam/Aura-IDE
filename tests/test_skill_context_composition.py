"""Production SINGLE prompt composition: skills, canonical context, custom prompts."""
from __future__ import annotations

from pathlib import Path

from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import (
    CONTEXT_PLACEHOLDER,
    FULL_REPLACEMENT_MARKER,
    SINGLE_SYSTEM_PROMPT,
    compose_system_prompt,
)

GODOT_TURN = dict(
    model="deepseek-chat",
    task_kind="bugfix",
    target_files=("scripts/player.gd",),
    content="fix the GDScript signal bug in the player scene physics process",
)


def _godot_workspace(tmp_path: Path) -> Path:
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "player.gd").write_text("extends Node3D\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return tmp_path


def _skill_entries(composed):
    return [entry for entry in composed.ledger if entry.kind == "individual_skill"]


def test_single_role_loads_terrain_selected_skills(tmp_path: Path) -> None:
    composed = compose_system_prompt(
        RuntimeRole.SINGLE, "", _godot_workspace(tmp_path), **GODOT_TURN
    )

    pack = next(entry for entry in composed.ledger if entry.source_id == "skill_pack")
    loaded = [entry for entry in _skill_entries(composed) if entry.included]

    assert pack.included is True
    assert loaded, "SINGLE must receive terrain-selected skills"
    assert any("godot_" in entry.reason for entry in loaded)
    assert "GDScript Practice" in composed.system_prompt


def test_ledger_records_skill_ids_reasons_and_char_counts(tmp_path: Path) -> None:
    composed = compose_system_prompt(
        RuntimeRole.SINGLE, "", _godot_workspace(tmp_path), **GODOT_TURN
    )

    entries = _skill_entries(composed)
    loaded = [entry for entry in entries if entry.included]
    skipped = [entry for entry in entries if not entry.included]

    assert all(entry.source_id.startswith("skill_") for entry in entries)
    assert all(entry.char_count > 0 for entry in loaded)
    assert all(entry.reason.strip() for entry in entries)
    assert skipped, "skipped skills must be reported with a reason"
    assert all(entry.char_count == 0 for entry in skipped)


def test_unrelated_request_does_not_carry_godot_guidance(tmp_path: Path) -> None:
    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        "",
        _godot_workspace(tmp_path),
        model="deepseek-chat",
        task_kind="refactor",
        target_files=("app.py",),
        content="rename the python handler function and add a docstring",
    )

    assert "GDScript Practice" not in composed.system_prompt
    assert "Godot Visual Iteration" not in composed.system_prompt


def test_non_godot_workspace_never_loads_the_godot_pack(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text("def handler():\n    return 1\n", encoding="utf-8")

    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        "",
        tmp_path,
        model="deepseek-chat",
        task_kind="bugfix",
        target_files=("service.py",),
        content="fix the gdscript signal bug in the godot scene",
    )

    assert "GDScript Practice" not in composed.system_prompt
    assert not [
        entry
        for entry in _skill_entries(composed)
        if entry.included and "godot_" in entry.reason
    ]


def test_startup_composition_without_terrain_loads_no_skills(tmp_path: Path) -> None:
    composed = compose_system_prompt(RuntimeRole.SINGLE, "", _godot_workspace(tmp_path))

    assert "GDScript Practice" not in composed.system_prompt
    assert "Core kernel:" in composed.system_prompt
    assert not [entry for entry in _skill_entries(composed) if entry.included]


def test_custom_prompt_extends_canonical_context_instead_of_replacing_it(
    tmp_path: Path,
) -> None:
    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        "Always answer in British English.",
        _godot_workspace(tmp_path),
        **GODOT_TURN,
    )

    assert "Always answer in British English." in composed.system_prompt
    assert "Core kernel:" in composed.system_prompt
    assert "Response discipline:" in composed.system_prompt
    assert "GDScript Practice" in composed.system_prompt


def test_edited_default_prompt_is_not_injected_twice(tmp_path: Path) -> None:
    custom = SINGLE_SYSTEM_PROMPT + "\n\nExtra house rule: never use emoji."

    composed = compose_system_prompt(
        RuntimeRole.SINGLE, custom, _godot_workspace(tmp_path), **GODOT_TURN
    )

    assert composed.system_prompt.count("Core kernel:") == 1
    assert composed.system_prompt.count("Response discipline:") == 1
    assert composed.system_prompt.count("### Godot 4.x GDScript Practice") == 1
    assert "never use emoji" in composed.system_prompt
    assert CONTEXT_PLACEHOLDER not in composed.system_prompt


def test_full_replacement_requires_the_documented_marker(tmp_path: Path) -> None:
    workspace = _godot_workspace(tmp_path)

    replaced = compose_system_prompt(
        RuntimeRole.SINGLE,
        f"{FULL_REPLACEMENT_MARKER}\nYou are a terse bot. {CONTEXT_PLACEHOLDER}",
        workspace,
        **GODOT_TURN,
    )

    assert "You are a terse bot." in replaced.system_prompt
    assert "Response discipline:" not in replaced.system_prompt
    assert "Core kernel:" in replaced.system_prompt
