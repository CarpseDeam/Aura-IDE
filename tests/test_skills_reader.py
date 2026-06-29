import json

from aura.skills.models import Skill, SkillProvenance
from aura.skills.reader import _read_user_authored_skills, read_skills


def test_read_user_authored_skills_missing_dir_returns_empty(tmp_path):
    assert _read_user_authored_skills(tmp_path) == []


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
    (authored_dir / "authored.json").write_text(
        json.dumps({"text": "Authored standard."}),
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
