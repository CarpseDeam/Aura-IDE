"""Godot bundled skill pack: packaged surface, lean terrain selection.

The pack ships four runtime-owned skills.  Selection is deliberately narrow:

* ``godot_gdscript`` — the baseline for `.gd` work (path-driven plus GDScript
  signals), never for the domain-agnostic ``bugfix`` task kind alone.
* ``godot_validation`` — selected by validation/test task kinds, headless
  execution, imports/``.uid``, parse/registration failures, or an explicit
  verification request — never merely because a `.gd` file is involved.
* ``godot_scene_architecture`` — selected by architecture signals (ownership,
  restructuring, wiring, signals/groups/Resources/autoloads, node-tree design)
  — never merely because a `.tscn` file is involved.
* ``godot_mmo_performance`` — specialty-selected through explicit signals.

The retired live-editor and asset-assembly skills (``godot_3d_assembly``,
``godot_aura_workflow``) are gone: not discoverable, not injectable.
"""
from __future__ import annotations

from pathlib import Path

from aura.context_gearbox.runtime import compose_system_prompt
from aura.skills.models import Skill, SkillProvenance
from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills
from aura.skills.text import build_skill_pack

SOURCE = Path("aura/skills/bundled")
VALIDATION_FILE = SOURCE / "godot_validation" / "SKILL.md"

PACKAGED_IDS = {
    "godot_gdscript",
    "godot_validation",
    "godot_scene_architecture",
    "godot_mmo_performance",
}
RETIRED_IDS = {"godot_3d_assembly", "godot_aura_workflow"}


def _workspace_with_skills(tmp_path: Path) -> Path:
    """A Godot workspace: the packaged pack loads from the project marker."""
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    return tmp_path


def _ids(skills) -> list[str]:
    return [dict(skill.origin).get("skill_id", "") for skill in skills]


def _by_id(skills) -> dict[str, Skill]:
    return {dict(skill.origin)["skill_id"]: skill for skill in skills}


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _selected_ids(
    skills,
    *,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
) -> list[str]:
    return _ids(
        select_relevant_skills(
            skills,
            task_kind=task_kind,
            target_files=target_files,
            content=content,
        )
    )


# ── packaged surface ────────────────────────────────────────────────────────


def test_four_godot_skills_are_packaged_for_a_godot_workspace(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    godot = [skill for skill in skills if dict(skill.origin).get("skill_id", "").startswith("godot_")]

    assert len(godot) == 4
    assert all(skill.provenance == SkillProvenance.BUNDLED for skill in godot)
    assert {dict(skill.origin)["skill_id"] for skill in godot} == PACKAGED_IDS


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


def test_godot_pack_ships_as_runtime_owned_skills_with_one_copy() -> None:
    assert SOURCE.parts[:2] == ("aura", "skills")
    assert not Path("scripts/personal/godot_knowledge/skills").exists()
    assert not any(
        retired in path.read_text(encoding="utf-8", errors="ignore")
        for retired in sorted(RETIRED_IDS)
        for path in Path("aura").rglob("*.py")
    )


def test_gdscript_guidance_targets_current_godot_classdb() -> None:
    text = (SOURCE / "godot_gdscript" / "SKILL.md").read_text(encoding="utf-8")
    assert "### Godot 4.x GDScript Practice" in text
    assert "ClassDB is exact for the running editor version" in text
    assert "Godot 4.6 GDScript Practice" not in text


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


def test_plain_gd_bugfix_injects_only_the_gdscript_index(tmp_path: Path) -> None:
    """The efficiency goal: one `.gd` bugfix without architecture language
    injects exactly one skill, not the whole pack."""
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = _selected_ids(
        skills,
        task_kind="bugfix",
        target_files=("scripts/player.gd",),
        content="fix the player movement speed",
    )

    assert selected == ["godot_gdscript"]


# ── tightened selectors ─────────────────────────────────────────────────────


def test_validation_and_scene_architecture_are_not_path_driven(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    by_id = {dict(skill.origin)["skill_id"]: skill for skill in skills}

    assert by_id["godot_validation"].path_globs == ()
    assert by_id["godot_scene_architecture"].path_globs == ()
    # godot_gdscript keeps the `.gd` baseline path; nothing keeps `.tscn`.
    assert by_id["godot_gdscript"].path_globs == ("**/*.gd",)
    assert by_id["godot_mmo_performance"].path_globs == ()


def test_gdscript_is_not_selected_by_the_generic_bugfix_task_kind_alone(
    tmp_path: Path,
) -> None:
    """A bugfix in a Godot repo still needs a GDScript signal, not just the
    domain-agnostic ``bugfix`` task kind (a Python bugfix is also a bugfix)."""
    skills = read_skills(_workspace_with_skills(tmp_path))
    assert "gdscript" in _by_id(skills)["godot_gdscript"].task_kinds
    assert "bugfix" not in _by_id(skills)["godot_gdscript"].task_kinds


# ── guide selection tests ───────────────────────────────────────────────────


def test_basic_gd_bugfix_loads_gdscript_not_validation_automatically(
    tmp_path: Path,
) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = _selected_ids(
        skills,
        task_kind="bugfix",
        target_files=("scripts/player.gd",),
        content="fix the GDScript signal bug in the player scene",
    )

    assert selected[0] == "godot_gdscript"
    assert "godot_gdscript" in selected
    assert "godot_validation" not in selected


def test_basic_tscn_edit_does_not_load_scene_architecture_without_a_signal(
    tmp_path: Path,
) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = _selected_ids(
        skills,
        task_kind="coding",
        target_files=("scenes/level.tscn",),
        content="adjust the background sprite position in the scene file",
    )

    assert "godot_scene_architecture" not in selected
    assert selected == []


def test_scene_architecture_loads_on_an_architecture_signal(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = _selected_ids(
        skills,
        task_kind="coding",
        target_files=("scenes/level.tscn",),
        content="restructure the scene ownership and wire the autoload dependency injection",
    )

    assert selected[0] == "godot_scene_architecture"


def test_validation_request_loads_validation_first(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = _selected_ids(
        skills,
        task_kind="validation",
        target_files=("tests/test_player.gd",),
        content="run a headless Godot import before script tests and check class_name registration",
    )

    assert selected[0] == "godot_validation"
    assert "godot_validation" in selected
    assert "godot_gdscript" in selected  # the .gd test target still earns its baseline


def test_validation_skill_routes_first_for_godot_validation(tmp_path: Path) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = _selected_ids(
        skills,
        task_kind="godot_validation",
        target_files=("tests/test_player.gd",),
        content="run a headless Godot import before script tests and check class_name registration",
    )

    assert selected[0] == "godot_validation"
    assert "godot_gdscript" in selected


def test_performance_request_loads_the_mmo_performance_skill_first(
    tmp_path: Path,
) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))
    selected = _selected_ids(
        skills,
        task_kind="coding",
        target_files=("scripts/world/chunk.gd",),
        content="optimize MMO repeated props with MultiMesh LOD and occlusion",
    )

    assert selected[0] == "godot_mmo_performance"
    assert "godot_mmo_performance" in selected


def test_non_godot_task_inside_a_godot_repository_loads_no_godot_skills(
    tmp_path: Path,
) -> None:
    skills = read_skills(_workspace_with_skills(tmp_path))

    for terrain in (
        dict(
            task_kind="bugfix",
            target_files=("app.py",),
            content="fix the python handler function bug",
        ),
        dict(
            task_kind="refactor",
            target_files=("services/relay.py",),
            content="rename the relay handler and split the module",
        ),
    ):
        selected = _selected_ids(skills, **terrain)
        assert selected == [], f"expected no Godot skills for {terrain}"


# ── retired skills ──────────────────────────────────────────────────────────


def test_retired_live_editor_and_assembly_skills_are_not_discoverable_or_injectable(
    tmp_path: Path,
) -> None:
    assert not (SOURCE / "godot_3d_assembly" / "SKILL.md").exists()
    assert not (SOURCE / "godot_aura_workflow" / "SKILL.md").exists()

    skills = read_skills(_workspace_with_skills(tmp_path))
    discovered = set(_ids(skills))
    assert not (discovered & RETIRED_IDS)

    # Terrains that used to route to the retired skills now load nothing retired.
    retired_terrains = (
        dict(
            task_kind="visual iteration",
            target_files=("addons/aura_bridge/transport/bridge_server.gd",),
            content="build a ruined checkpoint with catalog assets beneath AuraPreview",
        ),
        dict(
            task_kind="3d",
            target_files=("scenes/preview.tscn",),
            content="assemble modular Node3D assets with sockets and placement",
        ),
    )
    for terrain in retired_terrains:
        selected = set(_selected_ids(skills, **terrain))
        assert not (selected & RETIRED_IDS), f"retired skill leaked for {terrain}"


# ── context ledger ──────────────────────────────────────────────────────────


def test_context_ledger_reports_selected_skills_and_their_character_cost(
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_skills(tmp_path)
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "player.gd").write_text("extends Node3D\n", encoding="utf-8")

    composed = compose_system_prompt(
        workspace,
        model="deepseek-chat",
        task_kind="bugfix",
        target_files=("scripts/player.gd",),
        content="fix the GDScript signal bug in the player scene physics process",
    )

    pack_entry = next(e for e in composed.ledger if e.source_id == "skill_pack")
    loaded = [e for e in composed.ledger if e.kind == "individual_skill" and e.included]
    skipped = [e for e in composed.ledger if e.kind == "individual_skill" and not e.included]

    assert pack_entry.included and pack_entry.char_count > 0
    assert loaded, "the ledger must report the selected skills"
    assert any(e.reason.startswith("godot_") for e in loaded)
    # Every selected bundled skill is an indexed candidate with a positive
    # character cost and a deterministic selection reason.
    assert all(e.char_count > 0 for e in loaded)
    assert all("candidate_indexed" in e.detail for e in loaded)
    assert all(e.reason.strip() for e in loaded)
    # Skipped skills stay visible with a reason and a zero character cost.
    assert any(e.detail == "skipped" for e in skipped)
    assert all(e.char_count == 0 for e in skipped)

    # The aggregate cost reconciles with the built pack exactly.
    pack = build_skill_pack(
        workspace,
        model="deepseek-chat",
        task_kind="bugfix",
        target_files=("scripts/player.gd",),
        content="fix the GDScript signal bug in the player scene physics process",
    )
    assert pack_entry.char_count == len(pack.text)
