"""read_skill_resource: resources are reachable only for an activated skill,
resolved only against that skill's own frozen source directory.

Covers the production tool end-to-end (ToolRegistry), the turn-state
resolver directly (inactive/unknown-id/traversal/absolute/sibling/symlink/
binary rejection), and that resolution never rescans or mutates the frozen
candidate set.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aura.conversation.tools.registry import ToolRegistry
from aura.skills.library import SkillLibrary
from aura.skills.models import compute_skill_id, skill_body_hash
from aura.skills.text import SkillCandidate, SkillPack
from aura.skills.turn_state import SkillTurnState, read_skill_resource_result


def _write_skill_with_resources(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: a resourceful skill\n---\n# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    (directory / "references").mkdir()
    (directory / "references" / "api.md").write_text("# API reference\n\nDetails here.\n", encoding="utf-8")
    (directory / "scripts").mkdir()
    (directory / "scripts" / "setup.py").write_text("print('hello')\n", encoding="utf-8")
    return directory


def _discover_one(tmp_path: Path, name: str, *, project_dir: Path | None = None):
    project_dir = project_dir or (tmp_path / "project_authored")
    lib = SkillLibrary(
        tmp_path / "workspace",
        project_dir=project_dir,
        personal_dir=tmp_path / "personal_authored",
        bundled_dir=tmp_path / "bundled",
    )
    skills, _diagnostics = lib.discover_effective_skills()
    return next(s for s in skills if s.install_id == f"project:{name}")


def _candidate_for(skill) -> SkillCandidate:
    skill_id = compute_skill_id(skill)
    return SkillCandidate(
        skill_id=skill_id,
        label=skill_id,
        provenance=skill.provenance.value,
        description=skill.description or "",
        reason="test selection",
        index_chars=10,
        body_chars=len(skill.text),
        body_hash=skill_body_hash(skill),
        has_resources=skill.has_resources,
        eager_guard=False,
        skill=skill,
    )


def _state_with(*skills) -> tuple[SkillTurnState, dict]:
    candidates = {}
    built = []
    for skill in skills:
        candidate = _candidate_for(skill)
        candidates[skill] = candidate.skill_id
        built.append(candidate)
    state = SkillTurnState(SkillPack(candidates=tuple(built)))
    return state, candidates


# ── activated resource reads ────────────────────────────────────────────────


def test_activated_skill_resource_reads_successfully(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "res-skill")
    skill = _discover_one(tmp_path, "res-skill")
    state, ids = _state_with(skill)
    skill_id = ids[skill]

    state.resolve([skill_id])
    result = read_skill_resource_result(state, skill_id, "references/api.md")

    assert result["ok"] is True
    assert "API reference" in result["content"]
    assert result["skill_id"] == skill_id
    assert "content_hash" in result  # reuses Aura's bounded read_file, hash included


def test_resource_read_is_read_only_of_the_body_it_describes(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "script-skill")
    skill = _discover_one(tmp_path, "script-skill")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    result = read_skill_resource_result(state, skill_id, "scripts/setup.py")
    assert result["ok"] is True
    assert result["content"].strip() == "print('hello')"


# ── inactive / unknown rejection ────────────────────────────────────────────


def test_resource_read_rejected_when_skill_not_activated(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "not-activated")
    skill = _discover_one(tmp_path, "not-activated")
    state, ids = _state_with(skill)
    skill_id = ids[skill]

    # Frozen-indexed but never activated via load_skills.
    result = read_skill_resource_result(state, skill_id, "references/api.md")
    assert result["ok"] is False
    assert "load_skills" in result["error"]


def test_resource_read_rejected_for_unknown_skill_id(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "known")
    skill = _discover_one(tmp_path, "known")
    state, ids = _state_with(skill)
    state.resolve([ids[skill]])

    result = read_skill_resource_result(state, "skill_doesnotexist", "references/api.md")
    assert result["ok"] is False


# ── traversal / absolute / sibling / symlink / binary rejection ────────────


def test_resource_traversal_is_rejected(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "guarded")
    skill = _discover_one(tmp_path, "guarded")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    result = read_skill_resource_result(state, skill_id, "../guarded/SKILL.md")
    assert result["ok"] is False


def test_resource_absolute_path_is_rejected(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "abs-guarded")
    skill = _discover_one(tmp_path, "abs-guarded")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    absolute = str((tmp_path / "project_authored" / "abs-guarded" / "SKILL.md").resolve())
    result = read_skill_resource_result(state, skill_id, absolute)
    assert result["ok"] is False


def test_sibling_skill_access_is_rejected(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_authored"
    _write_skill_with_resources(project_dir, "skill-a")
    _write_skill_with_resources(project_dir, "skill-b")
    skill_a = _discover_one(tmp_path, "skill-a", project_dir=project_dir)
    state, ids = _state_with(skill_a)
    skill_id = ids[skill_a]
    state.resolve([skill_id])

    result = read_skill_resource_result(state, skill_id, "../skill-b/references/api.md")
    assert result["ok"] is False


def test_resource_symlink_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation requires elevated privileges on Windows CI")
    directory = _write_skill_with_resources(tmp_path / "project_authored", "linked")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    (directory / "references" / "link.md").symlink_to(outside)

    skill = _discover_one(tmp_path, "linked")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    result = read_skill_resource_result(state, skill_id, "references/link.md")
    assert result["ok"] is False


def test_binary_resource_content_is_rejected_deterministically(tmp_path: Path) -> None:
    directory = _write_skill_with_resources(tmp_path / "project_authored", "binary-holder")
    (directory / "assets").mkdir()
    (directory / "assets" / "icon.bin").write_bytes(bytes([0xFF, 0xFE, 0x00, 0x01, 0x80, 0x81]))

    skill = _discover_one(tmp_path, "binary-holder")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    result = read_skill_resource_result(state, skill_id, "assets/icon.bin")
    assert result["ok"] is False
    assert "UTF-8" in result["error"]


def test_nonexistent_resource_is_rejected(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "sparse")
    skill = _discover_one(tmp_path, "sparse")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    result = read_skill_resource_result(state, skill_id, "references/does-not-exist.md")
    assert result["ok"] is False


# ── never rescans or alters the frozen candidate set ────────────────────────


def test_resolution_never_mutates_the_frozen_candidate_set(tmp_path: Path) -> None:
    _write_skill_with_resources(tmp_path / "project_authored", "frozen-check")
    skill = _discover_one(tmp_path, "frozen-check")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    before = state.candidates
    read_skill_resource_result(state, skill_id, "references/api.md")
    read_skill_resource_result(state, skill_id, "does/not/exist.md")
    after = state.candidates

    assert before == after
    assert before[0] is after[0]


# ── production tool end-to-end (ToolRegistry) ───────────────────────────────


def test_registry_handles_read_skill_resource_end_to_end(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_dir = tmp_path / "project_authored"
    _write_skill_with_resources(project_dir, "e2e-skill")

    lib = SkillLibrary(
        workspace,
        project_dir=project_dir,
        personal_dir=tmp_path / "personal_authored",
        bundled_dir=tmp_path / "bundled",
    )
    skill = next(s for s in lib.discover_effective_skills()[0] if s.install_id == "project:e2e-skill")
    state, ids = _state_with(skill)
    skill_id = ids[skill]
    state.resolve([skill_id])

    registry = ToolRegistry(workspace_root=workspace)
    registry.set_turn_skill_state(state)

    result = registry.execute(
        "read_skill_resource",
        {"skill_id": skill_id, "path": "references/api.md"},
        approval_cb=lambda *_a, **_k: True,
        skill_turn_state=state,
    )
    assert result.ok is True
    assert "API reference" in result.payload["content"]


def test_registry_rejects_read_skill_resource_with_no_frozen_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry(workspace_root=workspace)

    result = registry.execute(
        "read_skill_resource",
        {"skill_id": "skill_anything", "path": "references/api.md"},
        approval_cb=lambda *_a, **_k: True,
        skill_turn_state=None,
    )
    assert result.ok is False
