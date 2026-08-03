from __future__ import annotations

from pathlib import Path

from aura.skills.models import SkillProvenance
from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills
from aura.skills.text import build_skill_context

SOURCE = Path("aura/skills/bundled")
WORKFLOW_FILE = SOURCE / "godot_aura_workflow" / "SKILL.md"
VALIDATION_FILE = SOURCE / "godot_validation" / "SKILL.md"


def _workspace_with_skills(tmp_path: Path) -> Path:
    """A Godot workspace: the packaged pack loads from the project marker."""
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    return tmp_path


def _ids(skills) -> list[str]:
    return [dict(skill.origin).get("skill_id", "") for skill in skills]


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_six_godot_skills_are_packaged_for_a_godot_workspace(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    godot = [skill for skill in skills if dict(skill.origin).get("skill_id", "").startswith("godot_")]

    assert len(godot) == 6
    assert all(skill.provenance == SkillProvenance.BUNDLED for skill in godot)
    assert {dict(skill.origin)["skill_id"] for skill in godot} == {
        "godot_3d_assembly",
        "godot_aura_workflow",
        "godot_gdscript",
        "godot_mmo_performance",
        "godot_scene_architecture",
        "godot_validation",
    }


def test_godot_pack_is_gated_by_the_project_marker(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert not [
        skill
        for skill in read_skills(tmp_path)
        if dict(skill.origin).get("skill_id", "").startswith("godot_")
    ]

    for directory in sorted(SOURCE.glob("godot_*")):
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
        assert 'workspace_markers: ["project.godot"]' in text


def test_relevant_godot_skills_are_ordered_by_specialty(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))

    mmo = select_relevant_skills(
        skills,
        task_kind="coding",
        target_files=("scripts/world/chunk.gd",),
        content="optimize MMO repeated props with MultiMesh LOD and occlusion",
    )
    gdscript = select_relevant_skills(
        skills,
        task_kind="bugfix",
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


def test_validation_skill_routes_first_for_godot_validation(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = select_relevant_skills(
        skills,
        task_kind="godot_validation",
        target_files=("tests/test_player.gd",),
        content="run a headless Godot import before script tests and check class_name registration",
    )

    assert _ids(selected)[0] == "godot_validation"
    assert "godot_gdscript" in _ids(selected)


def test_godot_pack_ships_as_runtime_owned_skills_with_one_copy() -> None:
    assert SOURCE.parts[:2] == ("aura", "skills")
    assert not Path("scripts/personal/godot_knowledge/skills").exists()
    assert not any(
        "godot_aura_workflow" in path.read_text(encoding="utf-8", errors="ignore")
        for path in Path("aura").rglob("*.py")
    )


def test_gdscript_guidance_targets_current_godot_classdb() -> None:
    text = (SOURCE / "godot_gdscript" / "SKILL.md").read_text(encoding="utf-8")
    assert "### Godot 4.x GDScript Practice" in text
    assert "ClassDB is exact for the running editor version" in text
    assert "Godot 4.6 GDScript Practice" not in text


def test_workflow_is_a_concise_single_agent_contract() -> None:
    text = WORKFLOW_FILE.read_text(encoding="utf-8")
    lower = text.lower()

    assert "production agent owns inspection, mutation, evidence, and revision" in lower
    assert "planner" not in lower
    assert "worker" not in lower
    assert len(text) < 2_300


def test_workflow_preserves_live_composition_contract() -> None:
    text = WORKFLOW_FILE.read_text(encoding="utf-8")
    lower = text.lower()
    normalized = _normalized(lower)

    assert "catalog asset id" in lower
    assert "genuine `aurapreview` root" in lower
    assert "never save the scene unless" in lower
    assert "visible layers" in lower
    assert "inspect exact scene facts" in lower
    assert "capture_godot_asset_preview" in text
    assert "critique_godot_preview_local" in text
    assert "structural facts alone do not prove visual coherence" in normalized
    assert "evidence stops changing" in lower
    assert "tool failure prevents further work" in lower


def test_validation_skill_defines_import_and_completion_checks() -> None:
    text = VALIDATION_FILE.read_text(encoding="utf-8")
    normalized = _normalized(text)

    assert "real configured or discovered Godot executable" in normalized
    assert "confirm its version" in normalized
    assert "headless editor import first" in normalized
    assert "`.uid` files before tests" in normalized
    assert "`SCRIPT ERROR` and `ERROR:` even when Godot exits with code 0" in normalized
    assert "missing test summary" in normalized
    assert "completion signal or result count" in normalized


def test_visual_iteration_routes_to_workflow_first(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    result = select_relevant_skills(
        skills,
        task_kind="visual iteration",
        target_files=("addons/aura_bridge/transport/bridge_server.gd",),
        content="build a ruined checkpoint with catalog assets beneath AuraPreview",
    )

    assert _ids(result)[0] == "godot_aura_workflow"


def test_live_composition_context_uses_single_agent_workflow(tmp_path: Path) -> None:
    context = build_skill_context(
        _workspace_with_skills(tmp_path),
        task_kind="visual iteration",
        target_files=("addons/aura_bridge/transport/bridge_server.gd",),
        content=(
            "Design a broken gate with unequal towers, connected walls, a readable court, "
            "deliberate negative space, and structural rubble beneath AuraPreview."
        ),
    )

    # The initial context is a compact index: the workflow's descriptive title
    # survives as the skill description, but the full procedure body is not
    # preloaded (its distinctive contract fragment stays out of the prompt).
    assert "Godot Visual Iteration" in context
    assert "critique_godot_preview_local" not in context
    assert "Planner and Worker" not in context
