"""Tests for aura/skills/refinement.py — derive_refinement_candidates, persist_refinement."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from aura.skills.models import Skill, SkillProvenance
from aura.skills.reader import read_skills
from aura.skills.refinement import (
    RefinementCandidate,
    derive_refinement_candidates,
    persist_refinement,
)

# ---------------------------------------------------------------------------
# derive_refinement_candidates
# ---------------------------------------------------------------------------


def _create_outcome_db(workspace_root: Path, rows: list[dict]) -> None:
    """Create .aura/skill_outcomes.db with outcome_joins table and rows."""
    db_dir = workspace_root / ".aura"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "skill_outcomes.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outcome_joins ("
        "  tool_call_id TEXT,"
        "  status TEXT,"
        "  task_kind TEXT,"
        "  target_files TEXT,"
        "  included_source_ids TEXT"
        ")"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO outcome_joins (tool_call_id, status, task_kind, target_files) "
            "VALUES (?, ?, ?, ?)",
            (row["tool_call_id"], row["status"], row["task_kind"], row["target_files"]),
        )
    conn.commit()
    conn.close()


def test_derive_refinement_candidates_works(tmp_path, monkeypatch):
    """Integration-style: DB with 3+ failures for a skill that matches terrain."""
    # 1. Create .aura/skill_outcomes.db
    db_dir = tmp_path / ".aura"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "skill_outcomes.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outcome_joins ("
        "  tool_call_id TEXT,"
        "  status TEXT,"
        "  task_kind TEXT,"
        "  target_files TEXT,"
        "  included_source_ids TEXT"
        ")"
    )
    # Insert 3 failed rows with distinct tool_call_ids, same task_kind 'refactor',
    # target_files '["aura/skills/models.py"]', status 'harness_error'
    for i in range(3):
        conn.execute(
            "INSERT INTO outcome_joins (tool_call_id, status, task_kind, target_files) "
            "VALUES (?, ?, ?, ?)",
            (f"fail_{i:03d}", "harness_error", "refactor", '["aura/skills/models.py"]'),
        )
    # Insert 1 success row to ensure filtering works
    conn.execute(
        "INSERT INTO outcome_joins (tool_call_id, status, task_kind, target_files) "
        "VALUES (?, ?, ?, ?)",
        ("success_001", "completed", "refactor", '["aura/skills/models.py"]'),
    )
    conn.commit()
    conn.close()

    # 2. Monkeypatch read_skills to return a Skill that matches refactor + aura/skills/
    monkeypatch.setattr(
        "aura.skills.refinement.read_skills",
        lambda wr: [
            Skill(
                text="test skill",
                task_kinds=("refactor",),
                path_globs=("aura/skills/",),
                model=None,
                provenance=SkillProvenance.BUNDLED,
                origin=(),
            )
        ],
    )

    result = derive_refinement_candidates(tmp_path, min_failures=3)

    assert len(result) == 1
    assert result[0].skill_text == "test skill"
    assert len(result[0].tool_call_ids) == 3
    assert result[0].task_kinds == ("refactor",)


def test_derive_refinement_candidates_no_db(tmp_path):
    """Empty workspace (no DB file) → empty list."""
    result = derive_refinement_candidates(tmp_path, min_failures=3)
    assert result == []


def test_derive_refinement_candidates_no_failures(tmp_path):
    """DB with only success rows returns []."""
    db_dir = tmp_path / ".aura"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "skill_outcomes.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outcome_joins ("
        "  tool_call_id TEXT,"
        "  status TEXT,"
        "  task_kind TEXT,"
        "  target_files TEXT"
        ")"
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO outcome_joins (tool_call_id, status, task_kind, target_files) "
            "VALUES (?, ?, ?, ?)",
            (f"ok_{i:03d}", "completed", "refactor", "[]"),
        )
    conn.commit()
    conn.close()

    result = derive_refinement_candidates(tmp_path, min_failures=3)
    assert result == []


def test_derive_refinement_candidates_below_min_failures(tmp_path, monkeypatch):
    """Only 2 distinct failure tool_call_ids, min_failures=3 → []."""
    db_dir = tmp_path / ".aura"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "skill_outcomes.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outcome_joins ("
        "  tool_call_id TEXT,"
        "  status TEXT,"
        "  task_kind TEXT,"
        "  target_files TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO outcome_joins (tool_call_id, status, task_kind, target_files) "
        "VALUES (?, ?, ?, ?)",
        ("f1", "harness_error", "refactor", '["aura/skills/models.py"]'),
    )
    conn.execute(
        "INSERT INTO outcome_joins (tool_call_id, status, task_kind, target_files) "
        "VALUES (?, ?, ?, ?)",
        ("f2", "validation_failed", "refactor", '["aura/skills/models.py"]'),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "aura.skills.refinement.read_skills",
        lambda wr: [
            Skill(
                text="test skill",
                task_kinds=("refactor",),
                path_globs=("aura/skills/",),
                model=None,
                provenance=SkillProvenance.BUNDLED,
                origin=(),
            )
        ],
    )

    result = derive_refinement_candidates(tmp_path, min_failures=3)
    assert result == []


# ---------------------------------------------------------------------------
# persist_refinement
# ---------------------------------------------------------------------------


def test_persist_refinement_creates_file_with_correct_metadata(tmp_path):
    """persist_refinement writes a JSON file with provenance=reflection_refined and preserved origin."""
    candidate = RefinementCandidate(
        skill_text="### Old Guard\nBe safe.",
        task_kinds=("refactor",),
        path_globs=("aura/skills/",),
        model="claude-sonnet-4-20250514",
        provenance_from="bundled",
        tool_call_ids=("abc123", "def456", "ghi789"),
    )
    rewritten = "### Improved Guard\nBe safer."

    dest = persist_refinement(tmp_path, candidate, rewritten)

    assert dest is not None
    assert dest.exists()
    assert dest.suffix == ".json"
    assert dest.parent == tmp_path / ".aura" / "skills" / "refined"

    data = json.loads(dest.read_text(encoding="utf-8"))

    # provenance
    assert data["provenance"] == "reflection_refined"

    # text
    assert data["text"] == rewritten

    # origin preserved
    origin = data["origin"]
    origin_dict = dict(origin)
    assert origin_dict.get("provenance_from") == "bundled"
    # original text prefix (first 120 chars)
    assert "Old Guard" in origin_dict.get("original_text_prefix", "")
    # triggering tool_call_ids
    trigger_ids = json.loads(origin_dict.get("triggering_tool_call_ids", "[]"))
    assert "abc123" in trigger_ids
    assert len(trigger_ids) == 3

    # task_kinds, path_globs, model are stored
    assert data["task_kinds"] == ["refactor"]
    assert data["path_globs"] == ["aura/skills/"]
    assert data["model"] == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# read_skills — inclusion of refined skills
# ---------------------------------------------------------------------------


def test_read_skills_includes_refined(tmp_path, monkeypatch):
    """read_skills must load refined JSON from .aura/skills/refined/*.json."""
    # Monkeypatch _read_graduated_skills to return [] to isolate test
    monkeypatch.setattr("aura.skills.reader._read_graduated_skills", lambda *a, **kw: [])

    refined_dir = tmp_path / ".aura" / "skills" / "refined"
    refined_dir.mkdir(parents=True, exist_ok=True)
    skill_id = hashlib.sha256("refined text".encode()).hexdigest()[:16]
    data = {
        "text": "refined text",
        "task_kinds": ["debug"],
        "path_globs": ["aura/"],
        "model": None,
        "provenance": "reflection_refined",
        "origin": [["provenance_from", "bundled"], ["original_text_prefix", "original text"]],
    }
    (refined_dir / f"{skill_id}.json").write_text(json.dumps(data), encoding="utf-8")

    skills = read_skills(tmp_path)

    # Should include refined skill
    refined_skills = [s for s in skills if s.provenance == SkillProvenance.REFLECTION_REFINED]
    assert len(refined_skills) >= 1
    assert refined_skills[0].text == "refined text"
    assert refined_skills[0].task_kinds == ("debug",)

    # Should also include bundled skills
    bundled_skills = [s for s in skills if s.provenance == SkillProvenance.BUNDLED]
    assert len(bundled_skills) >= 2  # drone_skill.json + gui_skill.json


def test_read_skills_returns_three_tiers(tmp_path, monkeypatch):
    """read_skills returns bundled + graduated + refined."""
    from aura.skills.models import Skill, SkillProvenance

    fake_graduated = [
        Skill(
            text="### Graduated Guard\nGraduated text.",
            task_kinds=("refactor",),
            path_globs=(),
            model=None,
            provenance=SkillProvenance.FAILURE_GRADUATED,
            origin=(("fingerprint", "fp1"),),
        )
    ]
    monkeypatch.setattr(
        "aura.skills.reader._read_graduated_skills", lambda *a, **kw: fake_graduated
    )

    refined_dir = tmp_path / ".aura" / "skills" / "refined"
    refined_dir.mkdir(parents=True)
    ref_data = {
        "text": "Refined Guard\nR.",
        "task_kinds": ["debug"],
        "path_globs": [],
        "model": None,
        "provenance": "reflection_refined",
        "origin": [],
    }
    (refined_dir / "test_refined.json").write_text(json.dumps(ref_data), encoding="utf-8")

    skills = read_skills(tmp_path)

    provenances = {s.provenance for s in skills}
    assert SkillProvenance.BUNDLED in provenances
    assert SkillProvenance.FAILURE_GRADUATED in provenances
    assert SkillProvenance.REFLECTION_REFINED in provenances

    # Verify at least one skill from each tier
    assert any(s.provenance == SkillProvenance.BUNDLED for s in skills)
    assert any(s.provenance == SkillProvenance.FAILURE_GRADUATED for s in skills)
    assert any(s.provenance == SkillProvenance.REFLECTION_REFINED for s in skills)
