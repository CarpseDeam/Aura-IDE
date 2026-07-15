from __future__ import annotations

import shutil
from pathlib import Path

from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills
from aura.skills.text import build_skill_context

SOURCE = Path("scripts/personal/godot_knowledge/skills")
SKILL_FILE = SOURCE / "godot_aura_workflow" / "SKILL.md"


def _skill_text() -> str:
    return SKILL_FILE.read_text(encoding="utf-8")


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
    authored = [
        skill for skill in read_skills(_workspace_with_skills(tmp_path))
        if dict(skill.origin).get("skill_id", "").startswith("godot_")
    ]
    assert {dict(skill.origin)["skill_id"] for skill in authored} == {
        "godot_3d_assembly", "godot_aura_workflow", "godot_gdscript",
        "godot_mmo_performance", "godot_scene_architecture",
    }


def test_personal_godot_skills_route_specialized_knowledge_first(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    assembly = select_relevant_skills(
        skills,
        task_kind="visual iteration",
        target_files=("addons/aura_bridge/transport/bridge_server.gd",),
        content="build a connected ruined gatehouse beneath AuraPreview",
    )
    assert _ids(assembly)[0] == "godot_aura_workflow"


def test_godot_workflow_skill_remains_outside_packaged_aura() -> None:
    assert SKILL_FILE.parts[0] == "scripts"
    assert not any(
        "godot_aura_workflow" in path.read_text(encoding="utf-8", errors="ignore")
        for path in Path("aura").rglob("*.py")
    )


def test_live_build_context_selects_workflow(tmp_path: Path) -> None:
    context = build_skill_context(
        _workspace_with_skills(tmp_path),
        task_kind="visual iteration",
        target_files=("addons/aura_bridge/transport/bridge_server.gd",),
        content="Build a ruined gatehouse with a real passage and connected walls beneath AuraPreview.",
    )
    assert "Godot Live Building — Natural Language Architectural Programs" in context
    assert "build_live_ruin" in context


def test_workflow_translates_plain_requests_into_one_architectural_program() -> None:
    text = _skill_text().lower()
    assert "read the user's requested place literally" in text
    assert "without making them learn procedural vocabulary" in text
    assert "never ask the user for operation names" in text
    assert "use `build_architectural_program`" in text
    assert "one program may contain several connected masses" in text
    assert "do not assemble a monastery, tower, gatehouse, or fortress" in text


def test_workflow_keeps_primary_tool_atomic_and_preview_incremental() -> None:
    text = _skill_text().lower()
    assert "`build_live_ruin` is the primary mutation tool" in text
    assert "exactly one cohesive semantic operation" in text
    assert "completed post-apply state before choosing the next operation" in text
    assert "continue beneath the existing real `aurapreview`" in text
    assert "preserve successful unaffected masses" in text
    assert "never save the scene unless explicitly requested" in text


def test_workflow_uses_only_plain_returned_facts_for_next_step() -> None:
    text = _skill_text().lower()
    assert "blueprint identity, mass handles, mass deltas, entrance facts, piece counts" in text
    assert "compact receipt is intentionally not a mesh or graph dump" in text
    assert "not treat returned metadata, operation names, or successful validation as proof" in text
    for removed in [
        "mass_map", "mass map", "vertical_profiles", "vertical profiles",
        "styling_affordances", "styling affordances", "structural_continuations",
        "structural continuation candidates", "primary low or wide mass",
        "secondary taller or narrower masses", "centre lies between",
        "repeated-footprint", "footprint transition",
    ]:
        assert removed not in text


def test_workflow_encodes_silhouette_facade_depth_and_coherent_ruin() -> None:
    text = _skill_text().lower()
    assert "make one mass visually dominant" in text
    assert "stepped or orthogonal footprints" in text
    assert "give long facades depth" in text
    assert "paired towers should differ" in text
    assert "concentrate ruin into one or two cause-and-effect regions" in text


def test_workflow_prefers_program_revisions_and_quarantines_legacy_operations() -> None:
    text = _skill_text()
    for operation in [
        "build_architectural_program", "inspect_architectural_program",
        "revise_architectural_mass", "revise_facade_grammar",
        "revise_vertical_profile", "revise_roof", "revise_ruin_profile",
        "revise_circulation", "apply_architectural_dressing",
        "export_architectural_blueprint",
    ]:
        assert operation in text
    lower = text.lower()
    assert "compatibility controls" in lower
    assert "never choose them for a new broad architectural request" in lower
    assert "stable blueprint/mass handles" in lower
    assert "godot `undoredo`" in lower


def test_workflow_uses_exact_diagnostics_and_optional_visual_checks() -> None:
    text = _skill_text().lower()
    assert "exact structured diagnostic and valid corrective candidates" in text
    assert "do not automatically call `capture_godot_asset_preview` or `critique_godot_preview_local`" in text
    assert "capturing an image alone is not visual analysis" in text
    assert "do not make vision a mandatory gate" in text


def test_workflow_forbids_new_construction_systems_and_implicit_save() -> None:
    text = _skill_text().lower()
    for forbidden_system in ["named building generators", "mandatory vision"]:
        assert forbidden_system in text
    assert "no raw tcp" in text
    assert "no arbitrary `.tscn` paths" in text
    assert "never save the scene unless explicitly requested" in text
