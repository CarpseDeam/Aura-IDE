"""SkillLibrary: sole owner of installed project/personal/bundled discovery.

Covers standard SKILL.md parsing (name/description, Aura's existing selection
metadata), legacy flat-JSON compatibility, duplicate-name precedence, stable
scope/name identity across body edits, persistent enable/disable, bundled
immutability, and that graduated/refined guards stay outside the managed
lifecycle entirely.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aura.skills.diagnostics import DiagnosticSeverity
from aura.skills.identity import InstalledSkillId, InstallScope
from aura.skills.library import SkillLibrary
from aura.skills.models import SkillProvenance
from aura.skills.reader import read_skills


def _write_skill(root: Path, name: str, *, frontmatter: str = "", body: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    text = body if body is not None else "# Title\n\nSome meaningful body text describing the procedure.\n"
    content = f"---\n{frontmatter}\n---\n{text}" if frontmatter else text
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    return directory


def _library(tmp_path: Path, **dirs) -> SkillLibrary:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    project_dir = dirs.pop("project_dir", tmp_path / "project_authored")
    personal_dir = dirs.pop("personal_dir", tmp_path / "personal_authored")
    bundled_dir = dirs.pop("bundled_dir", tmp_path / "bundled")
    return SkillLibrary(
        workspace,
        project_dir=project_dir,
        personal_dir=personal_dir,
        bundled_dir=bundled_dir,
    )


# ── standard SKILL.md parsing ───────────────────────────────────────────────


def test_standard_name_description_frontmatter_parses(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    _write_skill(
        project_dir,
        "my-skill",
        frontmatter='name: my-skill\ndescription: "Handles the widget pipeline."',
        body="# Widget Pipeline\n\nDo the widget thing carefully.\n",
    )
    lib = _library(tmp_path, project_dir=project_dir)
    skills, diagnostics = lib.discover_effective_skills()

    assert len(skills) == 1
    skill = skills[0]
    assert skill.description == "Handles the widget pipeline."
    assert skill.install_id == "project:my-skill"
    assert skill.provenance == SkillProvenance.USER_AUTHORED
    assert not any(d.is_error for d in diagnostics)


def test_existing_aura_metadata_fields_parse(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    _write_skill(
        project_dir,
        "with-metadata",
        frontmatter=(
            "name: with-metadata\n"
            "description: covers the metadata fields\n"
            'task_kinds: ["bugfix", "refactor"]\n'
            'path_globs: ["**/*.py"]\n'
            "model: deepseek-chat\n"
            'triggers: ["widget", "gizmo"]\n'
            'workspace_markers: ["pyproject.toml"]\n'
        ),
    )
    lib = _library(tmp_path, project_dir=project_dir)
    skills, _diagnostics = lib.discover_effective_skills()

    skill = skills[0]
    assert skill.task_kinds == ("bugfix", "refactor")
    assert skill.path_globs == ("**/*.py",)
    assert skill.model == "deepseek-chat"
    assert skill.triggers == ("widget", "gizmo")
    assert skill.workspace_markers == ("pyproject.toml",)


def test_malformed_frontmatter_reports_diagnostic_and_is_excluded(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    directory = project_dir / "broken"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: [unterminated\nbody text\n", encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    skills, diagnostics = lib.discover_effective_skills()

    assert skills == []
    assert any(d.severity == DiagnosticSeverity.ERROR for d in diagnostics)


def test_nested_resource_directories_are_detected(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    directory = _write_skill(project_dir, "with-resources")
    (directory / "references").mkdir()
    (directory / "references" / "api.md").write_text("reference content", encoding="utf-8")
    (directory / "scripts").mkdir()
    (directory / "scripts" / "setup.py").write_text("print('setup')", encoding="utf-8")
    (directory / "assets").mkdir()

    lib = _library(tmp_path, project_dir=project_dir)
    skills, _diagnostics = lib.discover_effective_skills()

    assert skills[0].has_resources is True
    assert skills[0].source_dir == directory


def test_no_resources_reports_false(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    _write_skill(project_dir, "plain")
    lib = _library(tmp_path, project_dir=project_dir)
    skills, _diagnostics = lib.discover_effective_skills()
    assert skills[0].has_resources is False


# ── legacy JSON compatibility ───────────────────────────────────────────────


def test_legacy_json_compatibility_through_library(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    project_dir.mkdir(parents=True)
    (project_dir / "legacy.json").write_text(
        '{"text": "Legacy guard text.", "task_kinds": ["bugfix"], "description": "legacy compat skill"}',
        encoding="utf-8",
    )

    lib = _library(tmp_path, project_dir=project_dir)
    skills, diagnostics = lib.discover_effective_skills()

    assert len(skills) == 1
    skill = skills[0]
    assert skill.text == "Legacy guard text."
    assert skill.provenance == SkillProvenance.USER_AUTHORED
    assert skill.install_id == "project:legacy"
    assert skill.description == "legacy compat skill"
    assert not any(d.is_error for d in diagnostics)

    # Reading never mutates the file it read.
    original = (project_dir / "legacy.json").read_text(encoding="utf-8")
    read_skills(tmp_path / "workspace")
    assert (project_dir / "legacy.json").read_text(encoding="utf-8") == original


def test_legacy_json_list_form_gets_distinct_identities(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    project_dir.mkdir(parents=True)
    (project_dir / "many.json").write_text(
        '[{"text": "first guard"}, {"text": "second guard"}]', encoding="utf-8"
    )
    lib = _library(tmp_path, project_dir=project_dir)
    skills, _diagnostics = lib.discover_effective_skills()

    ids = {s.install_id for s in skills}
    assert ids == {"project:many-0", "project:many-1"}


# ── discovery precedence ────────────────────────────────────────────────────


def test_project_personal_bundled_discovery_and_precedence(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    personal_dir = tmp_path / "personal_authored"
    bundled_dir = tmp_path / "bundled"

    _write_skill(project_dir, "shared", body="project version\n")
    _write_skill(personal_dir, "shared", body="personal version\n")
    _write_skill(bundled_dir, "shared", body="bundled version\n")

    lib = _library(tmp_path, project_dir=project_dir, personal_dir=personal_dir, bundled_dir=bundled_dir)
    skills, _diagnostics = lib.discover_effective_skills()

    assert len(skills) == 1
    assert "project version" in skills[0].text
    assert skills[0].install_id == "project:shared"


def test_personal_wins_over_bundled_when_no_project_version(tmp_path: Path) -> None:
    personal_dir = tmp_path / "personal_authored"
    bundled_dir = tmp_path / "bundled"
    _write_skill(personal_dir, "shared", body="personal version\n")
    _write_skill(bundled_dir, "shared", body="bundled version\n")

    lib = _library(tmp_path, personal_dir=personal_dir, bundled_dir=bundled_dir)
    skills, _diagnostics = lib.discover_effective_skills()

    assert len(skills) == 1
    assert "personal version" in skills[0].text
    assert skills[0].install_id == "personal:shared"


def test_duplicate_identity_within_one_scope_reports_diagnostic(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    _write_skill(project_dir, "dup")
    (project_dir / "dup.json").write_text('{"text": "also dup"}', encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    skills, diagnostics = lib.discover_effective_skills()

    assert len(skills) == 1  # the directory form wins; the json duplicate is dropped
    assert any(d.code == "duplicate_identity" for d in diagnostics)


# ── stable identity across body edits ───────────────────────────────────────


def test_installed_identity_is_stable_across_body_edits(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    directory = _write_skill(project_dir, "editable", body="original body\n")
    lib = _library(tmp_path, project_dir=project_dir)
    before = lib.discover_effective_skills()[0][0]

    (directory / "SKILL.md").write_text("---\n---\ncompletely different body now\n", encoding="utf-8")
    after = lib.discover_effective_skills()[0][0]

    assert before.install_id == after.install_id == "project:editable"
    from aura.skills.models import skill_body_hash

    assert skill_body_hash(before) != skill_body_hash(after)


# ── enable / disable persistence ────────────────────────────────────────────


def test_enable_disable_persists_across_library_instances(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    _write_skill(project_dir, "togglable")
    lib = _library(tmp_path, project_dir=project_dir)
    assert len(lib.discover_effective_skills()[0]) == 1

    lib.set_enabled("project:togglable", False)
    reloaded = _library(tmp_path, project_dir=project_dir)
    assert reloaded.discover_effective_skills()[0] == []
    assert reloaded.inspect("project:togglable").enabled is False

    reloaded.set_enabled("project:togglable", True)
    assert len(_library(tmp_path, project_dir=project_dir).discover_effective_skills()[0]) == 1


def test_disable_does_not_rewrite_skill_md(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    directory = _write_skill(project_dir, "untouched")
    original = (directory / "SKILL.md").read_text(encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    lib.set_enabled("project:untouched", False)

    assert (directory / "SKILL.md").read_text(encoding="utf-8") == original


# ── bundled immutability ────────────────────────────────────────────────────


def test_bundled_skills_can_be_disabled_locally(tmp_path: Path) -> None:
    bundled_dir = tmp_path / "bundled"
    _write_skill(bundled_dir, "packaged")
    lib = _library(tmp_path, bundled_dir=bundled_dir)
    assert len(lib.discover_effective_skills()[0]) == 1

    lib.set_enabled("bundled:packaged", False)
    assert lib.discover_effective_skills()[0] == []


def test_bundled_skills_cannot_be_uninstalled(tmp_path: Path) -> None:
    bundled_dir = tmp_path / "bundled"
    _write_skill(bundled_dir, "packaged")
    lib = _library(tmp_path, bundled_dir=bundled_dir)

    with pytest.raises(ValueError):
        lib.uninstall("bundled:packaged")
    assert (bundled_dir / "packaged" / "SKILL.md").is_file()


def test_project_skill_can_be_uninstalled(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    directory = _write_skill(project_dir, "removable")
    lib = _library(tmp_path, project_dir=project_dir)

    lib.uninstall("project:removable")
    assert not directory.exists()
    assert lib.discover_effective_skills()[0] == []


# ── broken skills stay visible to management ────────────────────────────────


def test_invalid_skill_md_is_listed_with_diagnostics_but_never_loaded(tmp_path: Path) -> None:
    """Runtime drops it; the inventory keeps it, so it can be fixed."""
    project_dir = tmp_path / "project_authored"
    _write_skill(project_dir, "healthy")
    broken = project_dir / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [unterminated\nbody text\n", encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    skills, _diagnostics = lib.discover_effective_skills()
    assert [s.install_id for s in skills] == ["project:healthy"]

    rows = {row.installed_id: row for row in lib.list_installed(InstallScope.PROJECT)}
    assert set(rows) == {"project:healthy", "project:broken"}
    assert rows["project:healthy"].valid is True
    assert rows["project:broken"].valid is False
    assert rows["project:broken"].source_dir == broken
    assert any(d.is_error for d in rows["project:broken"].diagnostics)


def test_inspect_returns_diagnostics_for_a_broken_entry_instead_of_none(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    broken = project_dir / "unparseable"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [unterminated\n", encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    inspected = lib.inspect("project:unparseable")

    assert inspected is not None
    assert inspected.valid is False
    assert inspected.body_chars == 0
    assert any(d.is_error for d in inspected.diagnostics)
    assert lib.inspect("project:never-existed") is None


def test_a_broken_entry_can_be_uninstalled(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    broken = project_dir / "removable-wreck"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nnot: [valid\n", encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    lib.uninstall("project:removable-wreck")

    assert not broken.exists()
    assert lib.list_installed(InstallScope.PROJECT) == []


def test_a_broken_entry_can_be_replaced_by_a_working_import(tmp_path: Path) -> None:
    from aura.skills.importer import SkillImporter

    project_dir = tmp_path / "project_authored"
    broken = project_dir / "fixable"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [unterminated\n", encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    source = _write_skill(
        tmp_path / "sources", "fixable", frontmatter="name: fixable\ndescription: the repaired version"
    )
    importer = SkillImporter(lib)
    preview = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)

    # The broken folder still occupies the name, so replacement stays explicit.
    assert preview.conflict is True
    with pytest.raises(Exception):
        importer.install(preview)

    repaired = importer.preview_from_folder(source, destination_scope=InstallScope.PROJECT)
    summary = importer.install(repaired, replace=True)

    assert summary.valid is True
    assert summary.description == "the repaired version"
    assert [s.install_id for s in lib.discover_effective_skills()[0]] == ["project:fixable"]


def test_malformed_legacy_json_is_listed_and_removable(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    project_dir.mkdir(parents=True)
    wreck = project_dir / "corrupt.json"
    wreck.write_text("{not valid json at all", encoding="utf-8")

    lib = _library(tmp_path, project_dir=project_dir)
    assert lib.discover_effective_skills()[0] == []

    rows = lib.list_installed(InstallScope.PROJECT)
    assert [(row.installed_id, row.valid) for row in rows] == [("project:corrupt", False)]
    assert rows[0].source_dir is None  # a file, not a folder — never exposed as a directory

    lib.uninstall("project:corrupt")
    assert not wreck.exists()


def test_a_broken_bundled_entry_is_listed_but_still_immutable(tmp_path: Path) -> None:
    bundled_dir = tmp_path / "bundled"
    broken = bundled_dir / "packaged-wreck"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [unterminated\n", encoding="utf-8")

    lib = _library(tmp_path, bundled_dir=bundled_dir)
    rows = lib.list_installed(InstallScope.BUNDLED)
    assert [(row.installed_id, row.valid) for row in rows] == [("bundled:packaged-wreck", False)]

    with pytest.raises(ValueError):
        lib.uninstall("bundled:packaged-wreck")
    assert broken.is_dir()


def test_a_broken_entry_does_not_shadow_a_working_one(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    personal_dir = tmp_path / "personal_authored"
    broken = project_dir / "shared"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [unterminated\n", encoding="utf-8")
    _write_skill(personal_dir, "shared", body="personal version\n")

    lib = _library(tmp_path, project_dir=project_dir, personal_dir=personal_dir)
    skills, _diagnostics = lib.discover_effective_skills()
    assert [s.install_id for s in skills] == ["personal:shared"]

    rows = {row.installed_id: row for row in lib.list_installed()}
    assert rows["personal:shared"].shadowed_by is None
    assert rows["project:shared"].valid is False


def test_read_skills_reports_discovery_diagnostics_without_leaking_them_into_prompts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Broken skills must not fail silently — but only to the log."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authored = workspace / ".aura" / "skills" / "authored"
    broken = authored / "silent-failure"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [unterminated\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="aura.skills.reader"):
        skills = read_skills(workspace)

    assert all(s.install_id != "project:silent-failure" for s in skills)
    assert any("silent-failure" in record.getMessage() for record in caplog.records)
    # Nothing about the failure reaches a composed skill body.
    assert not any("silent-failure" in s.text for s in skills)


# ── no duplicate discovery paths ────────────────────────────────────────────


def test_reader_no_longer_owns_authored_or_bundled_scanning() -> None:
    import aura.skills.reader as reader_module

    for retired in (
        "_read_bundled_skills",
        "_read_user_authored_skills",
        "_read_markdown_skill_dir",
        "_parse_skill_markdown",
        "_bundled_skills_dir",
    ):
        assert not hasattr(reader_module, retired), f"reader.py must not still define {retired}"


def test_identity_parse_round_trips() -> None:
    parsed = InstalledSkillId.parse("personal:my-skill")
    assert parsed == InstalledSkillId(scope=InstallScope.PERSONAL, name="my-skill")
    assert str(parsed) == "personal:my-skill"
    assert InstalledSkillId.parse("not-a-valid-id") is None
    assert InstalledSkillId.parse("nonsense:name") is None


# ── graduated / refined stay outside the managed lifecycle ─────────────────


def test_graduated_and_refined_guards_are_not_library_managed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    refined_dir = workspace / ".aura" / "skills" / "refined"
    refined_dir.mkdir(parents=True)
    (refined_dir / "guard.json").write_text(
        '{"text": "refined guard text", "provenance": "reflection_refined"}', encoding="utf-8"
    )

    lib = SkillLibrary(workspace, project_dir=tmp_path / "empty_project", personal_dir=tmp_path / "empty_personal", bundled_dir=tmp_path / "empty_bundled")
    installed, _diagnostics = lib.discover_effective_skills()
    assert installed == []
    assert lib.list_installed() == []

    # read_skills still surfaces the refined guard through its own dedicated path.
    all_skills = read_skills(workspace)
    assert any(s.provenance == SkillProvenance.REFLECTION_REFINED for s in all_skills)

    # InstallScope has no concept of a refined/graduated scope.
    assert {member.value for member in InstallScope} == {"project", "personal", "bundled"}
