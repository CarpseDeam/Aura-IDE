"""Progressive-disclosure repair: index-not-bodies, load_skills, pinning.

The two-stage skill lifecycle:
1. Deterministic candidate selection stays owned by Aura.
2. The initial skill context carries a compact index (authored/bundled) plus
   eagerly injected graduated/refined guard text — never full authored/bundled
   bodies.
3. Full bodies load only through the dedicated, read-only ``load_skills`` tool,
   resolving against the frozen per-turn candidate snapshot.
4. Activated bodies stay pinned for the rest of the real user turn.

These tests pin the delivery-stage contract. They deliberately do not retune
Aura's selector and do not touch the bundled Godot skill bodies.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import compose_system_prompt
from aura.conversation.api_view import RECEIPT_MARKER, build_api_view
from aura.conversation.tools.registry import ToolRegistry
from aura.hazard.models import HazardRecord
from aura.hazard.store import HazardStore
from aura.skills.reader import read_skills
from aura.skills.text import (
    SkillPack,
    SkillRecord,
    build_skill_context,
    build_skill_pack,
    format_skill_index,
    format_skills,
)
from aura.skills.turn_state import SkillTurnState, load_skills_result

GODOT_TURN = dict(
    model="deepseek-chat",
    task_kind="bugfix",
    target_files=("scripts/player.gd",),
    content="fix the GDScript signal bug in the player scene physics process",
)


def _godot_workspace(tmp_path: Path) -> Path:
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "player.gd").write_text("extends Node3D\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return tmp_path


def _authored_markdown_skill(
    tmp_path: Path,
    *,
    name: str = "gui_thread",
    description: str | None = None,
    body: str = "Marshal cross-thread widget updates through signals.\n",
) -> Path:
    skill_dir = tmp_path / ".aura" / "skills" / "authored" / name
    skill_dir.mkdir(parents=True)
    front_matter = "---\ntask_kinds: [bugfix]\npath_globs: [\"aura/gui/\"]\n"
    if description is not None:
        front_matter += f"description: {json.dumps(description)}\n"
    front_matter += "---\n"
    (skill_dir / "SKILL.md").write_text(front_matter + body, encoding="utf-8")
    return skill_dir


# ── 1. markdown and JSON description parsing ────────────────────────────────


def test_markdown_and_json_skills_parse_an_authored_description(tmp_path: Path) -> None:
    _authored_markdown_skill(
        tmp_path,
        description="Marshal Qt signals across threads safely.",
    )
    (tmp_path / ".aura" / "skills" / "authored" / "flat.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aura" / "skills" / "authored" / "flat.json").write_text(
        json.dumps(
            {
                "text": "Keep the GUI thread responsive.\nBlocking calls go to a worker.\n",
                "task_kinds": ["bugfix"],
                "description": "A flat JSON authored skill description.",
            }
        ),
        encoding="utf-8",
    )

    skills = read_skills(tmp_path)
    # Flat JSON skills carry no folder name, so they are found by stable id /
    # description rather than an origin skill_id.
    from aura.skills.models import compute_skill_id

    by_name = {
        dict(s.origin).get("skill_id") or compute_skill_id(s): s
        for s in skills
    }

    assert by_name["gui_thread"].description == "Marshal Qt signals across threads safely."
    flat = next(s for s in skills if s.description == "A flat JSON authored skill description.")
    assert flat is not None
    assert flat.text.startswith("Keep the GUI thread responsive.")


# ── 2. deterministic fallback descriptions ──────────────────────────────────


def test_fallback_description_uses_first_heading_and_paragraph(tmp_path: Path) -> None:
    _authored_markdown_skill(
        tmp_path,
        description=None,
        body=(
            "### Cross-Thread GUI Contract\n"
            "Marshal widget updates through signals.  Never touch the widget "
            "from a non-GUI thread, and keep the handler short.\n"
            "\n"
            "A second paragraph that must not appear in the one-line fallback.\n"
        ),
    )

    skills = read_skills(tmp_path)
    skill = skills[0]

    assert skill.description == (
        "Cross-Thread GUI Contract: Marshal widget updates through signals. "
        "Never touch the widget from a non-GUI thread, and keep the handler short."
    )


def test_fallback_description_is_one_compact_bounded_line(tmp_path: Path) -> None:
    body = (
        "### Long Title\n"
        + ("word " * 200)
        + "\n"
    )
    _authored_markdown_skill(tmp_path, description=None, body=body)

    skill = read_skills(tmp_path)[0]
    assert "\n" not in skill.description
    assert "  " not in skill.description  # whitespace collapsed
    assert len(skill.description) <= 160


# ── 3. malformed metadata fails closed ──────────────────────────────────────


def test_malformed_front_matter_fails_closed_for_that_skill(tmp_path: Path) -> None:
    _authored_markdown_skill(tmp_path, name="good", description="A good skill.")
    bad_dir = tmp_path / ".aura" / "skills" / "authored" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(
        "---\ntask_kinds: [bugfix]\n",  # missing closing delimiter
        encoding="utf-8",
    )

    # Reading must not crash; the malformed skill is simply absent.
    skills = read_skills(tmp_path)
    by_name = {dict(s.origin).get("skill_id", ""): s for s in skills}
    assert "good" in by_name
    assert "bad" not in by_name


def test_malformed_description_value_falls_back_without_crashing(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".aura" / "skills" / "authored" / "odd"
    skill_dir.mkdir(parents=True)
    # description declared as a JSON array — the wrong shape for a description.
    (skill_dir / "SKILL.md").write_text(
        '---\ntask_kinds: [bugfix]\ndescription: ["not", "a", "string"]\n---\n'
        "### Odd Skill\nUse the documented approach and nothing else.\n",
        encoding="utf-8",
    )

    skills = read_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].description == (
        "Odd Skill: Use the documented approach and nothing else."
    )


def test_malformed_metadata_never_crashes_prompt_composition(tmp_path: Path) -> None:
    bad_dir = tmp_path / ".aura" / "skills" / "authored" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text("---\n", encoding="utf-8")  # broken metadata
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")

    pack = build_skill_pack(tmp_path, **GODOT_TURN)
    assert isinstance(pack, SkillPack)


# ── 4. workspace-marker filtering remains intact ────────────────────────────


def test_workspace_marker_filtering_still_gates_bundled_skills(
    tmp_path: Path, monkeypatch
) -> None:
    import aura.skills.reader as reader_mod

    bundle_root = tmp_path / "bundle"
    skill_dir = bundle_root / "marker_gated"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nworkspace_markers: [\"project.godot\"]\n---\n"
        "### Marker Gated\nOnly loads where project.godot exists.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(reader_mod, "_bundled_skills_dir", lambda: bundle_root)

    without_marker = Path(tmp_path / "plain")
    without_marker.mkdir()
    (without_marker / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert all(
        dict(s.origin).get("skill_id") != "marker_gated"
        for s in read_skills(without_marker)
    )

    with_marker = Path(tmp_path / "godot")
    with_marker.mkdir()
    (with_marker / "project.godot").write_text("[application]\n", encoding="utf-8")
    ids = {dict(s.origin).get("skill_id") for s in read_skills(with_marker)}
    assert "marker_gated" in ids


# ── 5. initial context: candidate metadata, not authored/bundled bodies ─────


def test_initial_skill_context_contains_index_but_not_bundled_bodies(
    tmp_path: Path,
) -> None:
    pack = build_skill_pack(_godot_workspace(tmp_path), **GODOT_TURN)

    assert "### Skills" in pack.text
    assert "godot_gdscript" in pack.text  # human label
    assert "skill_" in pack.text  # stable id surface
    assert "reason:" in pack.text  # deterministic selection reason
    for candidate in pack.candidates:
        if candidate.eager_guard:
            continue  # guards are eagerly injected by design
        assert candidate.skill.text.strip() not in pack.text, (
            f"full body of {candidate.label} must not be preloaded"
        )
    assert "ClassDB is exact for the running editor version" not in pack.text


def test_authored_skill_bodies_are_indexed_not_preloaded(tmp_path: Path) -> None:
    skill_dir = _authored_markdown_skill(
        tmp_path,
        body=(
            "Marshal cross-thread widget updates through signals.\n"
            "Never touch the widget from a non-GUI thread.\n"
        ),
    )
    (tmp_path / "aura" / "gui").mkdir(parents=True)
    (tmp_path / "aura" / "gui" / "main_window.py").write_text("W = 1\n", encoding="utf-8")

    context = build_skill_context(
        tmp_path,
        task_kind="bugfix",
        target_files=("aura/gui/main_window.py",),
        content="fix the gui button threading",
    )

    assert "gui_thread" in context  # label is indexed
    assert "Marshal cross-thread widget updates through signals." in context  # description
    assert "Never touch the widget from a non-GUI thread." not in context  # body absent


# ── 6. graduated/refined eager guards remain present ────────────────────────


def _seed_graduated_hazard(workspace: Path) -> None:
    store = HazardStore(workspace / ".aura" / "hazards.db")
    base = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        for i in range(3):
            store.insert(
                HazardRecord(
                    model="deepseek-chat",
                    status="failed",
                    failure_class="RuntimeError",
                    target_files=("scripts/player.gd",),
                    task_kind="bugfix",
                    error_signature="signal connection failed",
                    raw_errors=("boom",),
                    tool_call_id=f"tc-{i}",
                    created_at=(base + timedelta(minutes=i)).isoformat(
                        timespec="seconds"
                    ),
                )
            )
    finally:
        store.close()


def test_graduated_and_refined_guards_are_eagerly_injected(tmp_path: Path) -> None:
    workspace = _godot_workspace(tmp_path)
    _seed_graduated_hazard(workspace)
    refined_dir = workspace / ".aura" / "skills" / "refined"
    refined_dir.mkdir(parents=True)
    (refined_dir / "guard.json").write_text(
        json.dumps(
            {
                "text": "Never call queue_free() twice on the same node.",
                "task_kinds": ["bugfix"],
            }
        ),
        encoding="utf-8",
    )

    composed = compose_system_prompt(RuntimeRole.SINGLE, "", workspace, **GODOT_TURN)
    context = composed.system_prompt

    assert "Never call queue_free() twice on the same node." in context
    assert "known biter" in context  # the graduated guard line is injected

    # The ledger classifies them as eager guards, distinct from candidates.
    eager = [
        entry
        for entry in composed.ledger
        if entry.kind == "individual_skill"
        and entry.detail is not None
        and entry.detail.startswith("eager_guard")
    ]
    indexed = [
        entry
        for entry in composed.ledger
        if entry.kind == "individual_skill"
        and entry.detail is not None
        and entry.detail.startswith("candidate_indexed")
    ]
    assert eager, "graduated/refined guards must appear as eager_guard entries"
    assert indexed, "authored/bundled candidates must appear as candidate_indexed entries"


# ── 7. deterministic ordering and stable IDs ────────────────────────────────


def test_ordering_is_deterministic_and_preserves_precedence(tmp_path: Path) -> None:
    workspace = _godot_workspace(tmp_path)
    authored = _authored_markdown_skill(
        workspace,
        body=(
            "### Custom Godot Rule\n"
            "Prefer scene composition over deep node hierarchies.\n"
        ),
    )
    # force the authored skill to also match this terrain
    (authored / "SKILL.md").write_text(
        "---\ntask_kinds: [bugfix]\npath_globs: [\"**/*.gd\"]\n---\n"
        "### Custom Godot Rule\nPrefer scene composition over deep node hierarchies.\n",
        encoding="utf-8",
    )

    first = build_skill_pack(workspace, **GODOT_TURN)
    second = build_skill_pack(workspace, **GODOT_TURN)

    assert first.text == second.text
    assert first.skill_ids == second.skill_ids
    assert all(candidate.skill_id.startswith("skill_") for candidate in first.candidates)

    # The frozen candidate snapshot follows provenance precedence:
    # workspace-authored before bundled, so the index and load_skills echo the
    # same scoping priority.
    provenance_order = [candidate.provenance for candidate in first.candidates]
    user_authored_index = (
        provenance_order.index("user_authored")
        if "user_authored" in provenance_order
        else len(provenance_order)
    )
    bundled_index = (
        provenance_order.index("bundled")
        if "bundled" in provenance_order
        else len(provenance_order)
    )
    assert user_authored_index < bundled_index


# ── 8. only frozen current-turn candidate IDs may be loaded ─────────────────


def test_load_skills_resolves_only_against_the_frozen_index(tmp_path: Path) -> None:
    workspace = _godot_workspace(tmp_path)
    frozen = SkillTurnState(build_skill_pack(workspace, **GODOT_TURN))

    # A candidate from a *different* selection (different terrain) is not part
    # of this turn's frozen index and must be rejected.
    other = SkillTurnState(
        build_skill_pack(
            workspace,
            task_kind="validation",
            target_files=("tests/test_player.gd",),
            content="run headless godot tests and check class registration",
        )
    )
    foreign_id = next(
        candidate.skill_id for candidate in other.candidates
        if candidate.skill_id not in frozen.skill_ids
    )

    result = load_skills_result(frozen, [foreign_id])
    assert result["activated_count"] == 0
    assert result["rejected_count"] == 1
    assert result["rejected"][0]["status"] == "not_exposed_for_turn"


def test_load_skills_rejects_arbitrary_global_skill_ids(tmp_path: Path) -> None:
    frozen = SkillTurnState(build_skill_pack(_godot_workspace(tmp_path), **GODOT_TURN))
    result = load_skills_result(frozen, ["skill_0000000000000000"])
    assert result["rejected"][0]["status"] == "not_exposed_for_turn"


# ── 9. batch activation preserves candidate order ───────────────────────────


def test_batch_activation_preserves_deterministic_candidate_order(
    tmp_path: Path,
) -> None:
    frozen = SkillTurnState(build_skill_pack(_godot_workspace(tmp_path), **GODOT_TURN))
    ids = list(frozen.skill_ids)
    assert len(ids) >= 2

    result = load_skills_result(frozen, ids)
    assert result["activated_count"] == len(ids)
    assert [item["skill_id"] for item in result["skills"]] == ids
    assert all("body" in item and item["body"] for item in result["skills"])


# ── 10. duplicate activation is idempotent ──────────────────────────────────


def test_duplicate_activation_is_idempotent(tmp_path: Path) -> None:
    frozen = SkillTurnState(build_skill_pack(_godot_workspace(tmp_path), **GODOT_TURN))
    skill_id = frozen.skill_ids[0]

    first = load_skills_result(frozen, [skill_id])
    second = load_skills_result(frozen, [skill_id])

    assert first["skills"][0]["activated"] == "new"
    assert second["skills"][0]["activated"] == "already_active"
    assert first["skills"][0]["body_hash"] == second["skills"][0]["body_hash"]

    log = frozen.activation_log()
    new_records = [r for r in log if r["skill_id"] == skill_id and r["was_new"]]
    assert len(new_records) == 1


# ── 11. unknown, stale, malformed, unavailable ids fail truthfully ──────────


def test_unknown_and_malformed_ids_fail_truthfully(tmp_path: Path) -> None:
    frozen = SkillTurnState(build_skill_pack(_godot_workspace(tmp_path), **GODOT_TURN))

    result = load_skills_result(frozen, ["skill_unknown", "", "   "])
    assert result["rejected_count"] == 3
    by_id = {item["skill_id"]: item for item in result["rejected"]}
    assert by_id["skill_unknown"]["status"] == "not_exposed_for_turn"
    assert by_id["" ]["status"] == "malformed"
    assert by_id["   "]["status"] == "malformed"


def test_unavailable_candidate_body_fails_truthfully() -> None:
    from aura.skills.models import Skill, SkillProvenance, compute_skill_id

    empty_skill = Skill(
        text="",
        task_kinds=("bugfix",),
        path_globs=(),
        model=None,
        provenance=SkillProvenance.BUNDLED,
        origin=(("skill_id", "empty"),),
    )
    record = SkillRecord(
        skill_id=compute_skill_id(empty_skill),
        label="empty",
        provenance=SkillProvenance.BUNDLED.value,
        reason="selected",
        char_count=0,
    )
    from aura.skills.text import SkillCandidate

    candidate = SkillCandidate(
        skill_id=compute_skill_id(empty_skill),
        label="empty",
        provenance=SkillProvenance.BUNDLED.value,
        description="",
        reason="selected",
        index_chars=0,
        body_chars=0,
        body_hash="",
        has_resources=False,
        eager_guard=False,
        skill=empty_skill,
    )
    pack = SkillPack(
        text="### Skills\n- empty\n",
        index_chars=16,
        guard_chars=0,
        candidates=(candidate,),
        skipped=(record,),
    )
    frozen = SkillTurnState(pack)
    result = load_skills_result(frozen, [compute_skill_id(empty_skill)])
    assert result["rejected_count"] == 1
    assert result["rejected"][0]["status"] == "unavailable"


# ── 12. activated bodies are replayable, not retired or duplicated ──────────


def _assistant(calls, rc=""):
    return {
        "role": "assistant",
        "content": "thinking",
        "tool_calls": calls,
        "reasoning_content": rc,
    }


def _history_with_activation():
    skill_body = "GDScript practice body " + "x" * 5000
    messages = [
        {"role": "user", "content": "fix the gdscript bug"},
        _assistant(
            [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "load_skills",
                    "arguments": json.dumps({"skill_ids": ["skill_abc"]}),
                },
            }],
            rc="r1",
        ),
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": json.dumps(
                {"ok": True, "skills": [{"skill_id": "skill_abc", "body": skill_body}]}
            ),
        },
        _assistant(
            [{
                "id": "c2",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "player.gd"}),
                },
            }],
            rc="r2",
        ),
        {
            "role": "tool",
            "tool_call_id": "c2",
            "content": json.dumps({"ok": True, "content": "file" * 1000}),
        },
    ]
    return messages, skill_body


def test_activated_bodies_are_replayable_and_never_retired(tmp_path: Path) -> None:
    messages, skill_body = _history_with_activation()
    view = build_api_view("system prompt", messages, budget_tokens=2_000)

    results = [
        m
        for m in view.messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
    ]
    assert len(results) == 1, "the activated-skill result must appear exactly once"
    assert skill_body in results[0]["content"]
    assert "aura compacted" not in results[0]["content"]

    # It must not be folded into a retired-evidence receipt.
    assert all(
        skill_body not in m.get("content", "")
        for m in view.messages
        if m.get("content") and RECEIPT_MARKER in m.get("content", "")
    )

    # Byte-identical across rounds: stable for provider prefix caching.
    view2 = build_api_view("system prompt", messages, budget_tokens=2_000)
    c1 = next(
        m["content"]
        for m in view.messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
    )
    c2 = next(
        m["content"]
        for m in view2.messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
    )
    assert c1 == c2


# ── 13. system-prompt fingerprint unchanged after activation ────────────────


def test_system_prompt_fingerprint_is_stable_after_activation(tmp_path: Path) -> None:
    workspace = _godot_workspace(tmp_path)
    composed = compose_system_prompt(RuntimeRole.SINGLE, "", workspace, **GODOT_TURN)
    frozen_system = composed.system_prompt
    fingerprint = build_api_view(
        frozen_system, [{"role": "user", "content": "fix it"}], budget_tokens=100_000
    ).stats.system_prompt_fingerprint

    # Activating a skill mutates nothing about the frozen system prompt.
    state = SkillTurnState(build_skill_pack(workspace, **GODOT_TURN))
    load_skills_result(state, [state.skill_ids[0]])

    after = build_api_view(
        frozen_system, [{"role": "user", "content": "fix it"}], budget_tokens=100_000
    )
    assert after.stats.system_prompt_fingerprint == fingerprint
    assert after.messages[0]["content"] == frozen_system


# ── 14. cancellation preserves completed activations ────────────────────────


def test_cancellation_preserves_completed_activations(tmp_path: Path) -> None:
    from aura.client import ApiError, ContentDelta
    from aura.conversation.history import History
    from aura.conversation.manager import ConversationManager
    from aura.conversation.tools.registry import ToolRegistry

    workspace = _godot_workspace(tmp_path)
    history = History()
    history.set_system("You are Aura.")
    history.append_user_text("fix the gdscript bug")
    history.messages.append(
        _assistant(
            [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "load_skills",
                    "arguments": json.dumps({"skill_ids": ["skill_abc"]}),
                },
            }]
        )
    )
    body = "ACTIVATED_SKILL_BODY_SENTINEL" * 50
    history.append_tool_result(
        "c1",
        json.dumps({"ok": True, "skills": [{"skill_id": "skill_abc", "body": body}]}),
    )
    # The newest block is interrupted: a call with no result yet.
    history.messages.append(
        _assistant(
            [{
                "id": "c2",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "player.gd"}),
                },
            }]
        )
    )

    registry = ToolRegistry(workspace_root=workspace, mode="single")
    manager = ConversationManager(history, registry)

    events = []
    manager._cleanup_cancelled(lambda ev: events.append(ev))

    roles = [m["role"] for m in history.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    tool_results = [
        m
        for m in history.messages
        if m.get("role") == "tool"
    ]
    assert len(tool_results) == 2
    assert body in tool_results[0]["content"], "completed activation must survive cancel"
    assert any(isinstance(ev, ApiError) for ev in events)


# ── 15. representative Godot request: materially fewer initial chars ────────


def test_representative_godot_request_has_far_fewer_initial_chars(
    tmp_path: Path,
) -> None:
    pack = build_skill_pack(_godot_workspace(tmp_path), **GODOT_TURN)
    assert pack.candidates, "the representative turn must select skills"

    old_full_body = format_skills([c.skill for c in pack.candidates], limit=8)
    new_index = pack.text

    assert new_index and old_full_body
    assert len(new_index) < len(old_full_body) * 0.5, (
        f"index ({len(new_index)}) should be materially smaller than the old "
        f"full-body pack ({len(old_full_body)})"
    )


# ── ledger and observability ────────────────────────────────────────────────


def test_skill_pack_aggregate_entry_reports_index_and_guard_chars_separately(
    tmp_path: Path,
) -> None:
    workspace = _godot_workspace(tmp_path)
    _seed_graduated_hazard(workspace)

    composed = compose_system_prompt(RuntimeRole.SINGLE, "", workspace, **GODOT_TURN)
    pack_entry = next(e for e in composed.ledger if e.source_id == "skill_pack")

    assert pack_entry.detail is not None
    assert "index_chars=" in pack_entry.detail
    assert "guard_chars=" in pack_entry.detail
    assert pack_entry.char_count == len(
        build_skill_pack(workspace, **GODOT_TURN).text
    )


def test_activation_ledger_distinguishes_activated_and_failed(tmp_path: Path) -> None:
    state = SkillTurnState(build_skill_pack(_godot_workspace(tmp_path), **GODOT_TURN))
    good_id = state.skill_ids[0]

    load_skills_result(state, [good_id, "skill_unknown"])
    log = state.activation_log()

    activated = [r for r in log if r["status"] == "activated"]
    failed = [r for r in log if r["status"] == "activation_failure"]
    assert len(activated) == 1
    assert len(failed) == 1
    assert activated[0]["was_new"] is True
    assert activated[0]["activated_chars"] > 0
    assert activated[0]["body_hash"]
    assert failed[0]["skill_id"] == "skill_unknown"


def test_load_skills_through_the_registry_is_an_observation(tmp_path: Path) -> None:
    workspace = _godot_workspace(tmp_path)
    state = SkillTurnState(build_skill_pack(workspace, **GODOT_TURN))
    registry = ToolRegistry(workspace_root=workspace, mode="single")

    from aura.conversation.tools.effects import ToolEffect

    assert registry.tool_effect("load_skills") is ToolEffect.OBSERVATION
    names = {t.get("function", {}).get("name") for t in registry.tool_defs()}
    assert "load_skills" in names

    result = registry.execute(
        "load_skills",
        {"skill_ids": [state.skill_ids[0]]},
        approval_cb=lambda _req: None,
        skill_turn_state=state,
    )
    assert result.ok
    assert result.payload["skills"][0]["body"]


def test_load_skills_never_granted_without_a_frozen_turn(tmp_path: Path) -> None:
    workspace = _godot_workspace(tmp_path)
    registry = ToolRegistry(workspace_root=workspace, mode="single")
    result = registry.execute(
        "load_skills",
        {"skill_ids": ["skill_anything"]},
        approval_cb=lambda _req: None,
    )
    assert result.ok
    assert result.payload["rejected"][0]["status"] == "not_exposed_for_turn"
