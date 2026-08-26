"""Explicit skill activation: user-named installed skills join the frozen turn.

``build_skill_pack`` composes two selection paths. Automatic selection stays
terrain-driven and untouched. Explicit selection names installed skills by
their stable ``scope:name`` identity, includes their full bodies in the initial
context, does not spend the automatic limit, and starts the frozen turn already
active — with unresolvable references reported rather than silently dropped.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aura.skills.library import SkillLibrary
from aura.skills.text import (
    EXPLICIT_CANDIDATE_CONFLICT,
    EXPLICIT_MALFORMED,
    EXPLICIT_UNAVAILABLE,
    build_skill_pack,
    format_explicit_skill_entry,
)
from aura.skills.turn_state import (
    STATUS_ACTIVATED,
    STATUS_EXPLICIT_PREACTIVATED,
    SkillTurnState,
    load_skills_result,
    read_skill_resource_result,
)

EXPLICIT_HEADER = "### Explicitly Selected Skills"
INDEX_HEADER = "### Skills"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated workspace whose personal/bundled state never leaks in."""
    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "workspace"
    (root / ".aura" / "skills" / "authored").mkdir(parents=True)
    return root


def _install(
    workspace: Path,
    name: str,
    *,
    body: str = "Do the careful thing, then verify it.",
    description: str = "a focused installed skill",
    task_kinds: tuple[str, ...] = (),
    triggers: tuple[str, ...] = (),
    with_resources: bool = False,
) -> Path:
    """Install one project-scope SKILL.md folder, as SkillLibrary discovers it."""
    directory = workspace / ".aura" / "skills" / "authored" / name
    directory.mkdir(parents=True, exist_ok=True)
    front = [f"name: {name}", f"description: {description}"]
    if task_kinds:
        front.append("task_kinds: [" + ", ".join(task_kinds) + "]")
    if triggers:
        front.append("triggers: [" + ", ".join(triggers) + "]")
    (directory / "SKILL.md").write_text(
        "---\n" + "\n".join(front) + f"\n---\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    if with_resources:
        (directory / "references").mkdir(exist_ok=True)
        (directory / "references" / "api.md").write_text(
            "# API reference\n\nDetails here.\n", encoding="utf-8"
        )
    return directory


def _write_refined_guard(workspace: Path, name: str, text: str) -> None:
    refined = workspace / ".aura" / "skills" / "refined"
    refined.mkdir(parents=True, exist_ok=True)
    (refined / f"{name}.json").write_text(
        '{"text": ' + f'"{text}"' + ', "task_kinds": ["bugfix"]}',
        encoding="utf-8",
    )


def _explicit(pack) -> list:
    return list(pack.explicit_candidates)


def _automatic(pack) -> list:
    return [candidate for candidate in pack.candidates if not candidate.explicit]


# ── explicit activation, with and without terrain ───────────────────────────


def test_explicit_skill_is_selected_with_no_terrain_signal_at_all(
    workspace: Path,
) -> None:
    _install(workspace, "python-testing", body="Always run pytest -q first.")

    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )

    assert [c.install_id for c in _explicit(pack)] == ["project:python-testing"]
    assert _automatic(pack) == [], "no terrain must still select nothing automatically"
    assert pack.unresolved_explicit == ()
    assert "Always run pytest -q first." in pack.text


def test_explicit_skill_is_selected_alongside_terrain_selected_skills(
    workspace: Path,
) -> None:
    _install(workspace, "python-testing", body="Always run pytest -q first.")
    _install(workspace, "bug-hunting", body="Reproduce before fixing.", task_kinds=("bugfix",))

    pack = build_skill_pack(
        workspace,
        task_kind="bugfix",
        explicit_install_ids=("project:python-testing",),
    )

    assert [c.install_id for c in _explicit(pack)] == ["project:python-testing"]
    assert [c.label for c in _automatic(pack)] == ["bug-hunting"]
    # The explicit section leads; the automatic index follows it.
    assert pack.text.index(EXPLICIT_HEADER) < pack.text.index(INDEX_HEADER)


# ── full-body prompt inclusion ──────────────────────────────────────────────


def test_explicit_skill_contributes_its_full_body_under_an_explicit_section(
    workspace: Path,
) -> None:
    body = "Step one: write the failing test.\nStep two: make it pass.\nStep three: refactor."
    _install(workspace, "python-testing", body=body, description="test-first discipline")

    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )

    assert EXPLICIT_HEADER in pack.text
    assert "already active" in pack.text
    for line in body.splitlines():
        assert line in pack.text
    assert "project:python-testing" in pack.text
    assert "test-first discipline" in pack.text
    assert "Supporting resources: none" in pack.text
    # Character accounting the Context Gearbox ledger needs, per candidate and
    # for the section as a whole.
    candidate = _explicit(pack)[0]
    assert candidate.explicit_chars > len(body)
    assert candidate.index_chars == 0 and candidate.guard_chars == 0
    assert candidate.body_chars == len(candidate.skill.text)
    assert pack.explicit_chars >= candidate.explicit_chars
    assert pack.index_chars == 0 and pack.guard_chars == 0
    # The compatibility record projection reports the explicit contribution.
    assert pack.selected[0].char_count == candidate.explicit_chars


def test_explicit_prompt_exposes_both_ids_and_resource_availability(
    workspace: Path,
) -> None:
    _install(
        workspace,
        "python-testing",
        body="Always run pytest -q first.",
        with_resources=True,
    )

    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )
    candidate = _explicit(pack)[0]
    entry = format_explicit_skill_entry(candidate.skill, candidate.install_id)

    assert "Installed identity: project:python-testing" in pack.text
    assert f"Candidate skill_id: {candidate.skill_id}" in pack.text
    assert "Supporting resources: present" in pack.text
    assert "read_skill_resource" in pack.text
    assert candidate.explicit_chars == len(entry)
    assert pack.explicit_chars == len(pack.text)


def test_explicit_candidate_keeps_its_content_derived_id_and_hash(
    workspace: Path,
) -> None:
    from aura.skills.models import compute_skill_id, skill_body_hash
    from aura.skills.reader import read_skills

    _install(workspace, "python-testing", body="Always run pytest -q first.")
    skill = next(
        s for s in read_skills(workspace) if s.install_id == "project:python-testing"
    )

    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )
    candidate = _explicit(pack)[0]

    assert candidate.skill_id == compute_skill_id(skill)
    assert candidate.skill_id.startswith("skill_")
    assert candidate.body_hash == skill_body_hash(skill)
    # The stable installed identity is carried separately, never conflated
    # with the content-addressed per-turn candidate id.
    assert candidate.install_id == "project:python-testing"
    assert candidate.install_id != candidate.skill_id


# ── supplied ordering and duplicate references ──────────────────────────────


def test_explicit_selection_order_is_preserved_and_duplicates_collapse(
    workspace: Path,
) -> None:
    _install(workspace, "zebra", body="Zebra procedure.")
    _install(workspace, "alpha", body="Alpha procedure.")

    pack = build_skill_pack(
        workspace,
        explicit_install_ids=(
            "  project:zebra  ",
            "project:alpha",
            "project:zebra",
        ),
    )

    assert [c.install_id for c in _explicit(pack)] == ["project:zebra", "project:alpha"]
    assert pack.text.index("Zebra procedure.") < pack.text.index("Alpha procedure.")
    assert pack.text.count("Zebra procedure.") == 1
    assert pack.unresolved_explicit == ()


def test_same_body_different_installs_report_conflict_and_freeze_first_resources(
    workspace: Path,
) -> None:
    first_dir = _install(
        workspace,
        "first",
        body="Shared procedure.",
        with_resources=True,
    )
    second_dir = _install(
        workspace,
        "second",
        body="Shared procedure.",
        with_resources=True,
    )
    # Make the parsed SKILL.md bodies byte-identical while keeping distinct
    # installed identities and resource roots.
    shared_body = "# Shared\n\nShared procedure.\n"
    for directory, name in ((first_dir, "first"), (second_dir, "second")):
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: shared\n---\n{shared_body}",
            encoding="utf-8",
        )
    (first_dir / "references" / "api.md").write_text(
        "first installation resource\n", encoding="utf-8"
    )
    (second_dir / "references" / "api.md").write_text(
        "second installation resource\n", encoding="utf-8"
    )

    first_pack = build_skill_pack(
        workspace,
        explicit_install_ids=("project:first", "project:second"),
    )

    assert [candidate.install_id for candidate in _explicit(first_pack)] == [
        "project:first"
    ]
    assert "project:second" not in first_pack.text
    assert len(first_pack.unresolved_explicit) == 1
    conflict = first_pack.unresolved_explicit[0]
    assert (conflict.reference, conflict.status) == (
        "project:second",
        EXPLICIT_CANDIDATE_CONFLICT,
    )
    assert "project:first" in conflict.reason
    assert first_pack.candidates[0].skill_id in conflict.reason

    first_read = read_skill_resource_result(
        SkillTurnState(first_pack),
        first_pack.candidates[0].skill_id,
        "references/api.md",
    )
    assert first_read["ok"] is True
    assert first_read["content"].strip() == "first installation resource"

    reversed_pack = build_skill_pack(
        workspace,
        explicit_install_ids=("project:second", "project:first"),
    )
    assert [candidate.install_id for candidate in _explicit(reversed_pack)] == [
        "project:second"
    ]
    assert [(item.reference, item.status) for item in reversed_pack.unresolved_explicit] == [
        ("project:first", EXPLICIT_CANDIDATE_CONFLICT)
    ]
    assert reversed_pack.candidates[0].skill_id == first_pack.candidates[0].skill_id

    reversed_read = read_skill_resource_result(
        SkillTurnState(reversed_pack),
        reversed_pack.candidates[0].skill_id,
        "references/api.md",
    )
    assert reversed_read["ok"] is True
    assert reversed_read["content"].strip() == "second installation resource"


def test_explicit_skills_lead_the_automatically_selected_candidates(
    workspace: Path,
) -> None:
    _install(workspace, "auto-one", body="Auto one.", task_kinds=("bugfix",))
    _install(workspace, "picked", body="Picked by hand.")

    pack = build_skill_pack(
        workspace, task_kind="bugfix", explicit_install_ids=("project:picked",)
    )

    assert [c.label for c in pack.candidates] == ["picked", "auto-one"]
    assert pack.candidates[0].explicit is True
    assert pack.candidates[1].explicit is False


# ── unresolved references ───────────────────────────────────────────────────


def test_missing_reference_is_reported_unresolved_without_raising(
    workspace: Path,
) -> None:
    _install(workspace, "present", body="Present procedure.")

    pack = build_skill_pack(
        workspace,
        explicit_install_ids=("project:present", "project:not-installed"),
    )

    assert [c.install_id for c in _explicit(pack)] == ["project:present"]
    assert [(u.reference, u.status) for u in pack.unresolved_explicit] == [
        ("project:not-installed", EXPLICIT_UNAVAILABLE)
    ]
    assert pack.unresolved_explicit[0].reason


def test_malformed_reference_is_reported_not_activated(workspace: Path) -> None:
    _install(workspace, "present", body="Present procedure.")

    pack = build_skill_pack(
        workspace,
        explicit_install_ids=("not-an-installed-id", "nonsense:", "  "),
    )

    assert _explicit(pack) == []
    assert all(u.status == EXPLICIT_MALFORMED for u in pack.unresolved_explicit)
    assert [u.reference for u in pack.unresolved_explicit] == [
        "not-an-installed-id",
        "nonsense:",
        "",
    ]


def test_disabled_installed_skill_cannot_be_explicitly_activated(
    workspace: Path,
) -> None:
    _install(workspace, "python-testing", body="Always run pytest -q first.")
    SkillLibrary(workspace).set_enabled("project:python-testing", False)

    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )

    assert _explicit(pack) == []
    assert "Always run pytest -q first." not in pack.text
    assert [u.status for u in pack.unresolved_explicit] == [EXPLICIT_UNAVAILABLE]


def test_internal_guards_are_not_explicitly_referenceable(workspace: Path) -> None:
    """Graduated hazards and refined guards have no installed identity."""
    from aura.skills.reader import read_skills

    _write_refined_guard(workspace, "guard", "Never skip the import check.")
    guard = next(s for s in read_skills(workspace) if "import check" in s.text)
    assert guard.install_id is None, "the guard is loaded, but has no install id"

    pack = build_skill_pack(
        workspace,
        explicit_install_ids=("project:guard", "refined:guard"),
    )

    assert _explicit(pack) == []
    assert [u.status for u in pack.unresolved_explicit] == [
        EXPLICIT_UNAVAILABLE,
        EXPLICIT_MALFORMED,
    ]


def test_unresolved_references_survive_an_empty_library(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    empty = tmp_path / "empty"
    empty.mkdir()

    pack = build_skill_pack(empty, explicit_install_ids=("project:nothing",))

    assert pack.candidates == ()
    assert [u.reference for u in pack.unresolved_explicit] == ["project:nothing"]


# ── explicit skills do not spend the automatic allowance ────────────────────


def test_explicit_skills_do_not_consume_the_automatic_eight_skill_limit(
    workspace: Path,
) -> None:
    for index in range(10):
        _install(
            workspace,
            f"auto-{index:02d}",
            body=f"Automatic procedure number {index}.",
            task_kinds=("bugfix",),
        )

    baseline = build_skill_pack(workspace, task_kind="bugfix")
    assert len(baseline.candidates) == 8, "the automatic limit is still eight"

    pack = build_skill_pack(
        workspace, task_kind="bugfix", explicit_install_ids=("project:auto-00",)
    )

    assert len(_explicit(pack)) == 1
    assert len(_automatic(pack)) == 8, "explicit selection must not cost an auto slot"
    assert len(pack.candidates) == 9


def test_explicitly_selected_skill_is_not_also_an_automatic_candidate(
    workspace: Path,
) -> None:
    _install(
        workspace,
        "python-testing",
        body="Always run pytest -q first.",
        task_kinds=("bugfix",),
    )

    pack = build_skill_pack(
        workspace, task_kind="bugfix", explicit_install_ids=("project:python-testing",)
    )

    assert [c.label for c in pack.candidates] == ["python-testing"]
    assert pack.candidates[0].explicit is True
    assert pack.text.count("Always run pytest -q first.") == 1
    assert [r.skill_id for r in pack.skipped] == []
    assert pack.skill_ids == [pack.candidates[0].skill_id]


def test_explicit_skill_is_never_reported_as_skipped_without_terrain(
    workspace: Path,
) -> None:
    _install(workspace, "picked", body="Picked by hand.")
    _install(workspace, "ignored", body="Not picked.")

    pack = build_skill_pack(workspace, explicit_install_ids=("project:picked",))

    assert [c.label for c in _explicit(pack)] == ["picked"]
    assert [r.label for r in pack.skipped] == ["ignored"]


# ── preactivated frozen turn state ──────────────────────────────────────────


def test_explicit_candidate_starts_the_turn_already_active(workspace: Path) -> None:
    _install(workspace, "python-testing", body="Always run pytest -q first.")
    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )
    state = SkillTurnState(pack)
    skill_id = pack.candidates[0].skill_id

    assert state.is_active(skill_id) is True
    assert state.activated_ids == (skill_id,)

    record = state.activation_log()[0]
    assert record["status"] == STATUS_EXPLICIT_PREACTIVATED
    assert record["explicit"] is True
    assert record["install_id"] == "project:python-testing"
    assert record["was_new"] is True
    assert "explicitly selected by the user" in record["reason"]
    assert record["activated_chars"] == pack.candidates[0].body_chars


def test_automatic_candidate_still_starts_the_turn_inactive(workspace: Path) -> None:
    _install(workspace, "auto-one", body="Auto one.", task_kinds=("bugfix",))
    _install(workspace, "picked", body="Picked by hand.")

    state = SkillTurnState(
        build_skill_pack(
            workspace, task_kind="bugfix", explicit_install_ids=("project:picked",)
        )
    )
    automatic = next(c for c in state.candidates if not c.explicit)

    assert state.is_active(automatic.skill_id) is False
    assert len(state.activated_ids) == 1


def test_explicit_skill_resource_reads_without_a_preceding_load_skills(
    workspace: Path,
) -> None:
    _install(workspace, "python-testing", body="Always run pytest -q first.", with_resources=True)
    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )
    state = SkillTurnState(pack)
    skill_id = pack.candidates[0].skill_id

    result = read_skill_resource_result(state, skill_id, "references/api.md")

    assert result["ok"] is True
    assert "API reference" in result["content"]
    assert result["skill_id"] == skill_id


def test_redundant_load_skills_for_an_explicit_skill_is_inert(
    workspace: Path,
) -> None:
    _install(workspace, "python-testing", body="Always run pytest -q first.")
    pack = build_skill_pack(
        workspace, explicit_install_ids=("project:python-testing",)
    )
    state = SkillTurnState(pack)
    skill_id = pack.candidates[0].skill_id

    result = load_skills_result(state, [skill_id])

    assert result["rejected_count"] == 0
    assert result["skills"][0]["activated"] == "already_active"
    assert result["skills"][0]["body"] == pack.candidates[0].skill.text
    assert state.activated_ids == (skill_id,)
    assert state.candidates == pack.candidates

    statuses = [record["status"] for record in state.activation_log()]
    assert statuses == [STATUS_EXPLICIT_PREACTIVATED, STATUS_ACTIVATED]
    # The skill is still truthfully an explicit user selection on the second
    # record, even though this event was a redundant load_skills call.
    assert state.activation_log()[1]["explicit"] is True
    assert state.activation_log()[1]["was_new"] is False


# ── unchanged behavior with no explicit selection ───────────────────────────


def test_empty_explicit_selection_is_identical_to_omitting_the_argument(
    workspace: Path,
) -> None:
    _install(workspace, "auto-one", body="Auto one.", task_kinds=("bugfix",))
    _install(workspace, "auto-two", body="Auto two.", task_kinds=("refactor",))

    before = build_skill_pack(workspace, task_kind="bugfix")
    after = build_skill_pack(workspace, task_kind="bugfix", explicit_install_ids=())

    assert after == before
    assert after.explicit_chars == 0
    assert after.unresolved_explicit == ()
    assert after.candidates and all(not c.explicit for c in after.candidates)
    assert after.selected == before.selected


def test_empty_explicit_selection_leaves_the_no_terrain_pack_unchanged(
    workspace: Path,
) -> None:
    _install(workspace, "auto-one", body="Auto one.", task_kinds=("bugfix",))

    before = build_skill_pack(workspace)
    after = build_skill_pack(workspace, explicit_install_ids=())

    assert after == before
    assert after.text == ""
    assert [r.reason for r in after.skipped] == ["no turn terrain to select against"]


def test_automatic_candidates_remain_compact_index_entries(workspace: Path) -> None:
    _install(
        workspace,
        "auto-one",
        body="The full automatic body that must not be preloaded.",
        task_kinds=("bugfix",),
    )
    _install(workspace, "picked", body="Picked by hand.")

    pack = build_skill_pack(
        workspace, task_kind="bugfix", explicit_install_ids=("project:picked",)
    )
    automatic = next(c for c in pack.candidates if not c.explicit)

    assert automatic.index_chars > 0 and automatic.explicit_chars == 0
    assert INDEX_HEADER in pack.text
    assert "The full automatic body that must not be preloaded." not in pack.text
    assert pack.index_chars > 0

    # ...and it still requires load_skills to reach its body.
    state = SkillTurnState(pack)
    assert state.is_active(automatic.skill_id) is False
    result = load_skills_result(state, [automatic.skill_id])
    assert result["skills"][0]["activated"] == "new"


def test_eager_guards_keep_their_eager_behavior_alongside_explicit_skills(
    workspace: Path,
) -> None:
    _write_refined_guard(workspace, "guard", "Never skip the import check.")
    _install(workspace, "picked", body="Picked by hand.")

    pack = build_skill_pack(
        workspace, task_kind="bugfix", explicit_install_ids=("project:picked",)
    )
    guard = next(c for c in pack.candidates if c.eager_guard)

    assert guard.explicit is False
    assert guard.guard_chars > 0
    assert "Never skip the import check." in pack.text
    assert pack.guard_chars > 0
    # The guard was eagerly injected, so it is already usable — but the frozen
    # activation ledger still only preactivates the explicit selection.
    assert SkillTurnState(pack).activated_ids == (
        next(c for c in pack.candidates if c.explicit).skill_id,
    )
