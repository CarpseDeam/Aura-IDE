"""Production SINGLE trajectory lifecycle: segments, rollover, runaway.

These tests drive the *real* :class:`ConversationManager` over the real
:class:`ToolRegistry` and a real temporary workspace with a scripted model
backend, plus direct unit checks of the lifecycle owner and the one compaction
owner.  They prove the repair's contract:

* many unique observations are never treated as a loop;
* a model-budget-relative observation segment boundary rolls the internal
  trajectory over instead of returning to the user;
* the rollover preserves the task, the route, the evidence, the applied writes,
  the failed-write recovery state, and the request shape;
* implementation progress — an applied write, or a write attempt that entered
  concrete edit recovery — exits pre-mutation trajectory accounting;
* structured blocker / already-satisfied outcomes still terminate;
* cancellation integrity and compaction survive a rollover;
* the remote runaway ceiling ends the run as a harness failure, never a blocker.
"""

from __future__ import annotations

import json
import threading

import pytest

from aura.client import ApiError, ToolResult
from aura.conversation import single_trajectory as st
from aura.conversation.api_view import (
    TRAJECTORY_ROLLOVER_MARKER,
    build_api_view,
    is_real_user_message,
)
from aura.conversation.context_budget import resolve_model_budget
from aura.conversation.manager_tool_round import (
    _applied_write_paths,
    _edit_recovery_write_paths,
    _trajectory_facts,
)
from aura.conversation.pre_edit_loop_guard import DUPLICATE_READ_REASON
from aura.conversation.single_trajectory import (
    RoundFacts,
    SingleTrajectoryController,
    TrajectoryDecision,
)
from aura.conversation.tools.effects import ToolEffect
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    HYBRID_ROUTE,
    IMPLEMENTATION_ROUTE,
    RESEARCH_ROUTE,
    Recorder,
    ScriptedBackend,
    approve_all as approve,
    build_manager,
    final_round,
    make_workspace,
    read_round,
    run,
    tool_round,
    write_round,
)


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.fixture
def tiny_segment(monkeypatch):
    """Shrink the segment allowance to zero tokens.

    The allowance is model-budget-relative by construction, so a scripted turn
    against a small fixture workspace would never fill a real one.  The
    *policy* under test is "a spent segment rolls over", not the size of the
    constant, so the constants are shrunk rather than the workspace inflated.
    """
    monkeypatch.setattr(st, "MIN_OBSERVATION_SEGMENT_TOKENS", 0)
    monkeypatch.setattr(st, "OBSERVATION_SEGMENT_TURNOVERS", 0)


def rollover_capsules(manager) -> list[dict]:
    return [
        m for m in manager.history.messages
        if m.get(TRAJECTORY_ROLLOVER_MARKER)
    ]


def unproven_steers(manager) -> list[dict]:
    return [
        m for m in manager.history.messages
        if m.get("role") == "user"
        and "has not reached a truthful terminal outcome" in str(m.get("content", ""))
    ]


# ── 1. unique observation is never a loop ───────────────────────────────────


def test_many_unique_observations_never_trip_the_duplicate_guard(
    tmp_path, isolated_streams
):
    """Distinct files, distinct searches: the narrow duplicate gate never fires."""
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend(
        [read_round(f"r{i}", i) for i in range(14)]
        + [
            tool_round([("g1", "grep_search", {"query": "value"})]),
            tool_round([("g2", "grep_search", {"query": "module"})]),
            write_round("w1"),
            final_round("Updated notes.md."),
        ]
    )
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    rejected = [
        r for r in recorder.tool_results()
        if isinstance(r.result, str) and DUPLICATE_READ_REASON in r.result
    ]
    assert rejected == [], "unique observations must never be duplicate-gated"
    assert json.loads(
        {r.tool_call_id: r for r in recorder.tool_results()}["w1"].result
    )["applied"] is True


# ── 2-5. the internal rollover ──────────────────────────────────────────────


def test_spent_observation_segment_rolls_over_instead_of_returning(
    tmp_path, isolated_streams, tiny_segment
):
    """The boundary produces an internal segment, not a user-visible outcome."""
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        write_round("w1"),
        final_round("Updated notes.md."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    capsules = rollover_capsules(manager)
    assert capsules, "a spent observation segment must roll the trajectory over"
    # Not a user-visible outcome of any kind.
    assert recorder.of_type(ApiError) == []
    assert manager.last_turn_blocked_reason == ""
    assert manager.last_turn_harness_failure is False
    # The turn continued and finished its real work in the same send.
    assert json.loads(
        {r.tool_call_id: r for r in recorder.tool_results()}["w1"].result
    )["applied"] is True
    assert (workspace / "notes.md").read_text(encoding="utf-8").endswith("acted\n")


def test_rollover_capsule_is_not_a_user_message_and_does_not_reroute(
    tmp_path, isolated_streams, tiny_segment
):
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        write_round("w1"),
        final_round("Done."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")

    run(manager, Recorder())

    capsule = rollover_capsules(manager)[0]
    assert capsule["aura_internal"] is True
    assert is_real_user_message(capsule) is False
    # The user's own request stays the authoritative one for routing, research
    # policy, rewind, and the transcript.
    assert manager.history.latest_real_user_text() == "Update notes.md."
    real_users = [
        m for m in manager.history.messages if is_real_user_message(m)
    ]
    assert len(real_users) == 1, "a rollover must not create a new user turn"


def test_rollover_preserves_task_route_evidence_and_request_shape(
    tmp_path, isolated_streams, tiny_segment
):
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        write_round("w1"),
        final_round("Done."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")

    run(manager, Recorder())

    capsules = rollover_capsules(manager)
    assert capsules
    text = str(capsules[0]["content"])
    assert "Update notes.md." in text, "original request stays authoritative"
    assert "implementation / implementation" in text, "selected route is preserved"
    assert "mod_00.py" in text, "observed paths preserved"
    assert "Outstanding implementation action" in text, "the owed edit is restated"

    # Same model, same thinking mode, same stable catalog across the boundary.
    assert backend.all_requests_stable() == []
    assert backend.every_request_thinking() == "high"

    # Canonical history keeps every completed result byte-for-byte.
    tool_ids = [
        m["tool_call_id"] for m in manager.history.messages
        if m.get("role") == "tool"
    ]
    assert {"r0", "r1", "w1"} <= set(tool_ids)


def test_capsule_reconstructs_applied_writes_and_edit_recovery_state():
    """The capsule is rebuilt from durable facts, not from the retired detail."""
    c = controller()
    c.note_tool_round(RoundFacts(
        observation_tokens=10, observation_targets=("aura/loop.py",)
    ))
    c.note_tool_round(RoundFacts(applied_write_paths=("aura/loop.py",)))
    c.note_tool_round(RoundFacts(edit_recovery_paths=("aura/other.py",)))
    c.note_tool_round(RoundFacts(command_executed=True))

    text = c.capsule_text()
    assert "Update notes.md." in text
    assert "aura/loop.py" in text
    assert "Writes already applied this turn" in text
    assert "require edit recovery" in text
    assert "aura/other.py" in text
    assert "Commands/validation runs executed this turn: 1" in text

    # Durable state survives the segment boundary itself.
    c.begin_new_segment()
    assert c.applied_write_paths == ["aura/loop.py"]
    assert c.edit_recovery_paths == ["aura/other.py"]
    assert c.observed_targets == ["aura/loop.py"]
    assert c.commands_run == 1
    assert c.observation_tokens_since_progress == 0
    assert "aura/loop.py" in c.capsule_text()


def test_rollover_is_unrationed_and_never_returns_unfinished_work(
    tmp_path, isolated_streams, tiny_segment
):
    """More than one internal segment boundary in one real user turn."""
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend(
        [read_round(f"r{i}", i) for i in range(8)]
        + [write_round("w1"), final_round("Done.")]
    )
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    assert len(rollover_capsules(manager)) >= 2, "rollovers must not be rationed"
    assert recorder.of_type(ApiError) == []
    assert json.loads(
        {r.tool_call_id: r for r in recorder.tool_results()}["w1"].result
    )["applied"] is True


# ── 6-7. what counts as implementation progress ─────────────────────────────


def controller(*, engaged: bool = True, allowance: int = 1_000):
    return SingleTrajectoryController(
        user_request="Update notes.md.",
        route=IMPLEMENTATION_ROUTE,
        engaged=engaged,
        segment_allowance_tokens=allowance,
        budget_working_set_tokens=allowance * 4,
    )


def test_applied_write_resets_pre_mutation_trajectory_accounting():
    c = controller()
    c.note_tool_round(RoundFacts(observation_tokens=900))
    assert c.observation_tokens_since_progress == 900
    c.note_tool_round(
        RoundFacts(observation_tokens=50, applied_write_paths=("notes.md",))
    )
    assert c.observation_tokens_since_progress == 0
    assert c.implementation_progress_events == 1
    assert c.decide() is TrajectoryDecision.CONTINUE


def test_failed_write_that_enters_edit_recovery_is_implementation_movement():
    c = controller()
    c.note_tool_round(RoundFacts(observation_tokens=990))
    c.note_tool_round(
        RoundFacts(observation_tokens=40, edit_recovery_paths=("notes.md",))
    )
    assert c.observation_tokens_since_progress == 0
    assert c.decide() is TrajectoryDecision.CONTINUE
    assert c.edit_recovery_paths == ["notes.md"]


def test_more_observation_alone_is_never_progress():
    c = controller(allowance=100)
    for _ in range(3):
        c.note_tool_round(
            RoundFacts(observation_tokens=50, observation_targets=("a.py",))
        )
    assert c.implementation_progress_events == 0
    assert c.decide() is TrajectoryDecision.INTERNAL_ROLLOVER


def test_write_result_classification_splits_applied_from_edit_recovery():
    tasks = [
        {"id": "a", "name": "write_file", "args": {"path": "ok.py"}, "effect": ToolEffect.MUTATION},
        {"id": "b", "name": "write_file", "args": {"path": "bad.py"}, "effect": ToolEffect.MUTATION},
    ]
    results = {
        "a": {"id": "a", "result_payload": json.dumps({"ok": True, "applied": True})},
        "b": {
            "id": "b",
            "result_payload": json.dumps(
                {"ok": False, "applied": False, "failure_class": "edit_mechanics_old_str_not_found"}
            ),
        },
    }
    assert _applied_write_paths(tasks, results) == ["ok.py"]
    assert _edit_recovery_write_paths(tasks, results) == ["bad.py"]

    facts = _trajectory_facts(
        tasks=tasks,
        results_by_id=results,
        applied_write_paths=["ok.py"],
        blocker_succeeded=False,
        already_satisfied_succeeded=False,
    )
    assert facts.bears_implementation_progress() is True
    assert facts.observation_tokens == 0


def test_observation_cost_is_the_size_of_accepted_results():
    payload = json.dumps({"ok": True, "content": "x" * 4_000})
    tasks = [{"id": "r", "name": "read_file", "args": {"path": "m.py"}, "effect": ToolEffect.OBSERVATION}]
    facts = _trajectory_facts(
        tasks=tasks,
        results_by_id={"r": {"id": "r", "result_payload": payload}},
        applied_write_paths=[],
        blocker_succeeded=False,
        already_satisfied_succeeded=False,
    )
    assert facts.observation_tokens == len(payload) // 4
    assert facts.observation_targets == ("m.py",)


def test_segment_allowance_is_model_budget_relative():
    small = SingleTrajectoryController.for_turn(
        mode="single", read_only=False, route=IMPLEMENTATION_ROUTE,
        user_request="x", budget=resolve_model_budget("unknown-tiny-model"),
    )
    assert small.segment_allowance_tokens >= st.MIN_OBSERVATION_SEGMENT_TOKENS
    assert small.segment_allowance_tokens == max(
        st.MIN_OBSERVATION_SEGMENT_TOKENS,
        int(
            small.budget_working_set_tokens
            * 0.25
            * st.OBSERVATION_SEGMENT_TURNOVERS
        ),
    )


# ── 8-9. structured terminal outcomes still terminate ───────────────────────


def test_structured_blocker_still_terminates(tmp_path, isolated_streams):
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        read_round("r0", 0),
        tool_round([("b1", "report_blocker", {
            "blocker": "The deployment credentials are not available to Aura.",
        })]),
        final_round("I cannot proceed without credentials."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    assert "credentials" in manager.last_turn_blocked_reason
    assert manager.last_turn_harness_failure is False
    assert rollover_capsules(manager) == []


def test_structured_already_satisfied_still_terminates(
    tmp_path, isolated_streams
):
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        read_round("r0", 0),
        tool_round([("s1", "report_already_satisfied", {
            "evidence": "notes.md already contains the requested body.",
        })]),
        final_round("Already satisfied."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    assert manager.last_turn_already_satisfied is True
    assert manager.last_turn_blocked_reason == ""
    assert rollover_capsules(manager) == []


# ── 10. cancellation integrity across a rollover ────────────────────────────


def test_blocker_after_a_rollover_still_terminates_correctly(
    tmp_path, isolated_streams, tiny_segment
):
    """A segment boundary is not terminal and does not disturb a real outcome."""
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        tool_round([("b1", "report_blocker", {
            "blocker": "The deployment credentials are not available to Aura.",
        })]),
        final_round("I cannot proceed without credentials."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    assert rollover_capsules(manager), "the boundary was crossed first"
    assert "credentials" in manager.last_turn_blocked_reason
    assert manager.last_turn_harness_failure is False
    assert recorder.of_type(ApiError) == []


def test_cancellation_after_rollover_preserves_work_and_pairing(
    tmp_path, isolated_streams, tiny_segment
):
    workspace = make_workspace(tmp_path / "ws")
    cancel = threading.Event()
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        write_round("w1"),
        final_round("cancelled here"),
    ])

    def stream_then_cancel(**kwargs):
        events = list(backend.stream(**kwargs))
        if len(backend.calls) >= 4:
            # The write has landed and the final response is streaming: cancel
            # after the rollover and after real work completed.
            cancel.set()
        return iter(events)

    isolated_streams.register(PRODUCTION_STREAM_HOOK, stream_then_cancel)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    manager.send(
        on_event=recorder,
        approval_cb=approve,
        cancel_event=cancel,
        model="scripted-production-model",
        thinking="high",
        hook_name=PRODUCTION_STREAM_HOOK,
        task_route=IMPLEMENTATION_ROUTE,
    )

    assert rollover_capsules(manager), "the rollover happened before the cancel"
    messages = manager.history.messages
    # Completed reads and the applied write remain.
    tool_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    assert {"r0", "r1", "w1"} <= tool_ids
    assert (workspace / "notes.md").read_text(encoding="utf-8").endswith("acted\n")
    # Every assistant tool-call block is still fully paired.
    for index, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        paired = {
            m.get("tool_call_id") for m in messages[index + 1:]
            if m.get("role") == "tool"
        }
        for call in msg["tool_calls"]:
            assert call["id"] in paired, f"unpaired tool call {call['id']}"


# ── 11. compaction and rollover are distinct but compatible ─────────────────


def observation_block(call_id: str, path: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"ok": True, "content": "y" * 900}),
        },
    ]


def test_rollover_marker_retires_superseded_observation_via_the_one_ledger():
    """The trajectory owner marks the boundary; api_view remains the compactor."""
    messages: list[dict] = [{"role": "user", "content": "Update notes.md."}]
    messages += observation_block("r0", "a.py")
    messages += observation_block("r1", "b.py")
    messages += [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "w0",
                "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py"})},
            }],
        },
        {"role": "tool", "tool_call_id": "w0", "content": json.dumps({"ok": True, "applied": True})},
    ]
    messages.append({
        "role": "user",
        "content": "[Aura internal trajectory rollover]",
        "aura_internal": True,
        TRAJECTORY_ROLLOVER_MARKER: True,
    })
    messages += observation_block("r2", "c.py")

    # A budget far larger than the content: without the marker nothing retires.
    without = build_api_view(None, messages[:-3] + messages[-2:], 1_000_000)
    assert without.stats.rollover_retired_blocks == 0

    view = build_api_view(None, messages, 1_000_000)
    assert view.stats.rollover_retired_blocks == 2, "both pre-boundary reads retire"
    assert view.stats.ledger_entries >= 2, "into the one existing evidence ledger"
    # The applied mutation survives the boundary verbatim; the post-boundary
    # observation is the new segment's own active chain.
    assert view.residency.is_resident("w0") is True
    assert view.residency.is_resident("r0") is False
    assert view.residency.is_resident("r1") is False


def test_rollover_does_not_replace_ordinary_budget_compaction():
    """Ordinary allowance-driven retirement still happens without a marker."""
    messages: list[dict] = [{"role": "user", "content": "Update notes.md."}]
    for i in range(12):
        messages += observation_block(f"r{i}", f"m{i}.py")
    messages += observation_block("active", "z.py")

    view = build_api_view(None, messages, 2_000)
    assert view.stats.rollover_retired_blocks == 0
    assert view.stats.retired_blocks > 0, "the allowance still retires evidence"


# ── 12. the remote runaway ceiling ──────────────────────────────────────────


def test_runaway_ceiling_terminates_as_harness_failure_not_a_blocker(
    tmp_path, isolated_streams, monkeypatch
):
    monkeypatch.setattr(st, "SINGLE_RUNAWAY_TOOL_CALL_CEILING", 2)
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend(
        [read_round(f"r{i}", i) for i in range(8)] + [final_round("...")]
    )
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    errors = recorder.of_type(ApiError)
    assert errors, "the runaway boundary must end the run"
    assert "harness failure" in errors[-1].message
    assert manager.last_turn_harness_failure is True
    # Never a blocker, never a request to continue.
    assert manager.last_turn_blocked_reason == ""
    assert manager.last_turn_already_satisfied is False
    assert unproven_steers(manager) == [], (
        "no _UNPROVEN_CONTINUATION may be appended after the harness terminated"
    )
    # Completed work is preserved with valid pairing.
    tool_ids = {
        m["tool_call_id"] for m in manager.history.messages if m.get("role") == "tool"
    }
    assert {"r0", "r1"} <= tool_ids
    # The loop stopped: no further requests were issued past the ceiling.
    assert len(backend.calls) == 2


def test_runaway_ceiling_applies_before_engagement_is_even_consulted():
    c = controller(engaged=False)
    c.note_accepted_tool_calls(st.SINGLE_RUNAWAY_TOOL_CALL_CEILING)
    assert c.decide() is TrajectoryDecision.TERMINAL_HARNESS_FAILURE
    assert c.terminated_for_runaway is True


def test_prose_nonconvergence_rolls_over_rather_than_steering_forever(
    tmp_path, isolated_streams
):
    """_UNPROVEN_CONTINUATION is the first answer, not the only one."""
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        final_round("Here is my analysis."),
        final_round("Here is more analysis."),
        write_round("w1"),
        final_round("Done."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Update notes.md.")
    recorder = Recorder()

    run(manager, recorder)

    assert len(unproven_steers(manager)) == 1, "one cheap in-segment correction"
    assert rollover_capsules(manager), "the second nonconvergence rolls over"
    assert json.loads(
        {r.tool_call_id: r for r in recorder.tool_results()}["w1"].result
    )["applied"] is True


# ── 13-14. scope guarantees ─────────────────────────────────────────────────


def _imported_modules(module) -> set[str]:
    """Every module name the module's own import statements reference."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_worker_todo_has_no_authority_over_the_trajectory():
    """The display-only TODO is not wired into execution.

    Asserted on the import graph and the public API rather than on prose: the
    lifecycle owner must not read, write, or schedule from the TODO, and must
    expose no cursor or scheduling surface for anything else to drive it from.
    """
    imported = _imported_modules(st)
    assert not any("todo" in name.lower() for name in imported)
    assert not any("artifact" in name.lower() for name in imported)

    api = {name for name in dir(SingleTrajectoryController) if not name.startswith("_")}
    for forbidden in ("next_item", "advance", "cursor", "schedule", "current_task"):
        assert forbidden not in api, f"trajectory owner must expose no {forbidden}"


def test_no_checkpoint_focused_or_required_tool_machinery_was_introduced():
    """None of the forbidden protocols came back with the lifecycle owner."""
    import aura.conversation.manager as manager_module

    for module in (st, manager_module):
        imported = _imported_modules(module)
        assert not any("planner_dispatch_gate" in n for n in imported) or module is manager_module
    source = __import__("pathlib").Path(st.__file__).read_text(encoding="utf-8")
    for forbidden in ("tool_choice", "require_tool_call", "focused_action", "decision_checkpoint"):
        assert forbidden not in source


def test_read_only_and_observation_routes_are_never_pushed_toward_a_mutation():
    read_only = SingleTrajectoryController.for_turn(
        mode="single", read_only=True, route=IMPLEMENTATION_ROUTE,
        user_request="Show me the loop.", budget=resolve_model_budget(""),
    )
    assert read_only.engaged is False

    answer_only = SingleTrajectoryController.for_turn(
        mode="single", read_only=False, route=RESEARCH_ROUTE,
        user_request="What changed in the API?", budget=resolve_model_budget(""),
    )
    assert answer_only.engaged is False

    for c in (read_only, answer_only):
        c.note_tool_round(RoundFacts(observation_tokens=10_000_000))
        assert c.decide() is TrajectoryDecision.CONTINUE

    hybrid = SingleTrajectoryController.for_turn(
        mode="single", read_only=False, route=HYBRID_ROUTE,
        user_request="Look up the new API and wire it in.",
        budget=resolve_model_budget(""),
    )
    assert hybrid.engaged is True, "a hybrid coding turn still owes its edit"


def test_read_only_turn_ends_on_prose_without_rollover(tmp_path, isolated_streams, tiny_segment):
    workspace = make_workspace(tmp_path / "ws")
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        final_round("Here is what the loop does."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager = build_manager(workspace, "Explain the loop.", read_only=True)
    recorder = Recorder()

    run(manager, recorder)

    assert rollover_capsules(manager) == []
    assert unproven_steers(manager) == []
    assert "Here is what the loop does." in recorder.chat_text
