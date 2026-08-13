from pathlib import Path

from aura.context_gearbox.runtime import (
    PRODUCTION_SYSTEM_PROMPT,
    compose_system_prompt,
)


def test_production_prompt_composes_selected_skills_without_runtime_roles(
    tmp_path: Path,
) -> None:
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "player.gd").write_text("extends Node3D\n", encoding="utf-8")

    composed = compose_system_prompt(
        "",
        tmp_path,
        model="deepseek-chat",
        task_kind="bugfix",
        target_files=("scripts/player.gd",),
        content="fix the GDScript signal bug",
    )

    assert "GDScript Practice" in composed.system_prompt
    assert PRODUCTION_SYSTEM_PROMPT
    assert not hasattr(composed, "role")
