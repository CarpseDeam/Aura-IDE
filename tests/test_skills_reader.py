import json
from pathlib import Path

from aura.skills.models import Skill, SkillProvenance
from aura.skills.reader import _read_user_authored_skills, read_skills


def test_read_user_authored_skills_missing_dir_returns_empty(tmp_path):
    assert _read_user_authored_skills(tmp_path) == []


def test_read_user_authored_skills_loads_skill_md_folders_in_sorted_order(tmp_path):
    authored_dir = tmp_path / ".aura" / "skills" / "authored"
    authored_dir.mkdir(parents=True)
    (authored_dir / "zeta").mkdir()
    (authored_dir / "zeta" / "SKILL.md").write_text(
        "Zeta authored standard.",
        encoding="utf-8",
    )
    (authored_dir / "alpha").mkdir()
    (authored_dir / "alpha" / "SKILL.md").write_text(
        "\nAlpha authored standard.\n",
        encoding="utf-8",
    )
    (authored_dir / "empty").mkdir()
    (authored_dir / "empty" / "SKILL.md").write_text("   ", encoding="utf-8")
    (authored_dir / "missing_skill_md").mkdir()

    skills = _read_user_authored_skills(tmp_path)

    assert [skill.text for skill in skills] == [
        "Alpha authored standard.",
        "Zeta authored standard.",
    ]
    assert all(skill.provenance == SkillProvenance.USER_AUTHORED for skill in skills)
    assert skills[0].task_kinds == ()
    assert skills[0].path_globs == ()
    assert skills[0].model is None
    assert skills[0].origin == (("skill_id", "alpha"),)
    assert skills[1].origin == (("skill_id", "zeta"),)


def test_read_user_authored_skills_applies_skill_md_front_matter(tmp_path):
    authored_dir = tmp_path / ".aura" / "skills" / "authored"
    skill_dir = authored_dir / "safe_subprocess"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
task_kinds: ["coding"]
path_globs: ["aura/**/*.py"]
model: "test-model"
triggers: ["subprocess", "shell=True"]
---------------------------------------

Use argument-vector process launches for external tools.
""",
        encoding="utf-8",
    )

    skills = _read_user_authored_skills(tmp_path)

    assert len(skills) == 1
    skill = skills[0]
    assert skill.text == "Use argument-vector process launches for external tools."
    assert skill.task_kinds == ("coding",)
    assert skill.path_globs == ("aura/**/*.py",)
    assert skill.model == "test-model"
    assert skill.triggers == ("subprocess", "shell=True")
    assert skill.provenance == SkillProvenance.USER_AUTHORED
    assert skill.origin == (("skill_id", "safe_subprocess"),)


def test_read_user_authored_skills_malformed_front_matter_loads_plain_body(tmp_path):
    authored_dir = tmp_path / ".aura" / "skills" / "authored"
    skill_dir = authored_dir / "malformed"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
task_kinds ["coding"]
---

Malformed metadata should not block the skill body.
""",
        encoding="utf-8",
    )

    skills = _read_user_authored_skills(tmp_path)

    assert len(skills) == 1
    assert skills[0].text == "Malformed metadata should not block the skill body."
    assert skills[0].task_kinds == ()
    assert skills[0].path_globs == ()
    assert skills[0].model is None
    assert skills[0].triggers == ()


def test_read_user_authored_skills_loads_single_and_array_files(tmp_path):
    authored_dir = tmp_path / ".aura" / "skills" / "authored"
    authored_dir.mkdir(parents=True)
    (authored_dir / "00_single.json").write_text(
        json.dumps(
            {
                "text": "Single authored standard.",
                "task_kinds": ["bugfix"],
                "path_globs": ["aura/**"],
                "model": "test-model",
                "origin": [["author", "kori"]],
            }
        ),
        encoding="utf-8",
    )
    (authored_dir / "01_array.json").write_text(
        json.dumps(
            [
                {"text": "Array authored standard."},
                {"text": ""},
                "not an object",
            ]
        ),
        encoding="utf-8",
    )
    (authored_dir / "02_bad.json").write_text("{bad json}", encoding="utf-8")

    skills = _read_user_authored_skills(tmp_path)

    assert [skill.text for skill in skills] == [
        "Single authored standard.",
        "Array authored standard.",
    ]
    assert all(skill.provenance == SkillProvenance.USER_AUTHORED for skill in skills)
    assert skills[0].task_kinds == ("bugfix",)
    assert skills[0].path_globs == ("aura/**",)
    assert skills[0].model == "test-model"
    assert skills[0].origin == (("author", "kori"),)


def test_read_skills_returns_user_authored_first(tmp_path, monkeypatch):
    authored_dir = tmp_path / ".aura" / "skills" / "authored"
    authored_dir.mkdir(parents=True)
    (authored_dir / "authored").mkdir()
    (authored_dir / "authored" / "SKILL.md").write_text(
        "Authored standard.",
        encoding="utf-8",
    )
    bundled = [
        Skill(
            text="Bundled skill.",
            task_kinds=(),
            path_globs=(),
            model=None,
            provenance=SkillProvenance.BUNDLED,
            origin=(),
        )
    ]
    monkeypatch.setattr("aura.skills.reader._read_bundled_skills", lambda: bundled)
    monkeypatch.setattr("aura.skills.reader._read_graduated_skills", lambda *a, **kw: [])
    monkeypatch.setattr("aura.skills.reader._read_refined_skills", lambda *_a: [])

    skills = read_skills(tmp_path)

    assert [skill.provenance for skill in skills] == [
        SkillProvenance.USER_AUTHORED,
        SkillProvenance.BUNDLED,
    ]


def test_seeded_foundation_skills_are_user_authored():
    repo_root = Path(__file__).resolve().parents[1]
    skills = _read_user_authored_skills(repo_root)
    seeded_ids = {
        origin_value
        for skill in skills
        for origin_key, origin_value in skill.origin
        if origin_key == "skill_id"
    }

    assert {
        "coding-no-swallowed-success-errors",
        "coding-independent-verifier",
        "coding-io-at-edges",
        "coding-safe-subprocess",
        "coding-safe-sql",
        "coding-qt-thread-affinity",
        "coding-no-committed-secrets",
        "testing-facts-before-code",
        "planner-worker-boundary",
        "coding-one-seam-only",
    }.issubset(seeded_ids)
    for skill in skills:
        if any(origin[0] == "skill_id" and origin[1] in seeded_ids for origin in skill.origin):
            assert skill.provenance == SkillProvenance.USER_AUTHORED
