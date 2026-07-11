from __future__ import annotations

import shutil
from pathlib import Path

from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills

SOURCE = Path("scripts/personal/godot_knowledge/skills")


def _workspace_with_skills(tmp_path: Path) -> Path:
    destination = tmp_path / ".aura/skills/authored"
    destination.mkdir(parents=True)
    for source in SOURCE.iterdir():
        if source.is_dir():
            shutil.copytree(source, destination / source.name)
    return tmp_path


def _ids(skills) -> list[str]:
    return [dict(skill.origin).get("skill_id", "") for skill in skills]


def test_personal_godot_skills_are_read_as_project_authored(tmp_path: Path) -> None:
    root = _workspace_with_skills(tmp_path)
    skills = read_skills(root)
    authored = [skill for skill in skills if dict(skill.origin).get("skill_id", "").startswith("godot_")]

    assert len(authored) == 5
    assert {dict(skill.origin)["skill_id"] for skill in authored} == {
        "godot_3d_assembly",
        "godot_aura_workflow",
        "godot_gdscript",
        "godot_mmo_performance",
        "godot_scene_architecture",
    }


def test_personal_godot_skills_route_specialized_knowledge_first(tmp_path: Path) -> None:
    root = _workspace_with_skills(tmp_path)
    skills = read_skills(root)

    mmo = select_relevant_skills(
        skills,
        task_kind="coding",
        target_files=("scripts/world/chunk.gd",),
        content="optimize MMO repeated props with MultiMesh LOD and occlusion",
    )
    gdscript = select_relevant_skills(
        skills,
        task_kind="coding",
        target_files=("scripts/player.gd",),
        content="fix this GDScript signal and physics process bug",
    )
    assembly = select_relevant_skills(
        skills,
        task_kind="coding",
        target_files=("scenes/preview.tscn",),
        content="assemble modular Node3D assets with sockets and visual iteration",
    )

    assert _ids(mmo)[0] == "godot_mmo_performance"
    assert _ids(gdscript)[0] == "godot_gdscript"
    assert _ids(assembly)[:2] == ["godot_3d_assembly", "godot_aura_workflow"]


def test_personal_godot_knowledge_is_not_imported_by_packaged_aura() -> None:
    assert SOURCE.parts[0] == "scripts"
    assert not any(
        "godot_knowledge" in path.read_text(encoding="utf-8", errors="ignore")
        for path in Path("aura").rglob("*.py")
    )
