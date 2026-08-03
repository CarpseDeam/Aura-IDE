"""Hybrid implementation routing, with no discovery ceiling attached to it.

A coding request that also needs current external facts does not route to the
implementation lane.  It routes to::

    TaskLane.research, action="research_then_worker"

— the research lane, because the research has to happen first, not because the
turn stops there.  It still ends in an edit.

:func:`~aura.conversation.task_router.route_bears_production_action` is the one
shared predicate that recognises those turns, so
:func:`~aura.conversation.manager_send_state.implementation_action_pending` and
:func:`~aura.conversation.focused_action.should_enter_focused_action` cannot
disagree about which turns owe the workspace an act.  Two things still depend on
that predicate being right:

* a hybrid turn's probes do not falsely complete it before its first write;
* a hybrid turn is eligible for the focused action protocol when — and only
  when — the evidence rules say discovery has stopped moving.

What no longer depends on it is any *ceiling*.  The two-hop stage is gone, and
the tests below assert its absence directly: a hybrid turn and a route-less turn
both survey across more sequential rounds than that ceiling ever allowed, and
still reach their edit on an ordinary request.

The one behaviour preserved from the ceiling era is the missing-route
resolution: a send given no route still resolves one from the real user message,
so route-dependent behaviour never silently switches off because a caller forgot
an argument.
"""

from __future__ import annotations

import pytest

from aura.conversation.focused_action import (
    FocusedActionState,
    should_enter_focused_action,
)
from aura.conversation.manager_send_state import (
    _SendState,
    implementation_action_pending,
)
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import (
    TaskLane,
    TaskRoute,
    classify_user_request,
    route_bears_production_action,
)
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    Recorder,
    ScriptedBackend,
    build_manager,
    final_round,
    make_workspace,
    read_round,
    run,
    tool_round,
    write_round,
)


def _same_listing(call_id: str, *, path: str = "."):
    """One round whose result is identical whatever the argument spelling."""
    return tool_round([(call_id, "list_directory", {"path": path})])

#: A real coding request that also needs current external facts. Deliberately
#: not a hand-built route: the point is that the *classifier's own* answer for
#: this kind of request is recognised as a turn that owes an act.
HYBRID_REQUEST = (
    "Update notes.md to document the current Godot 4.5 signal syntax, "
    "checking the latest docs online"
)

#: A plain coding request, used where the route is omitted entirely.
CODING_REQUEST = "Update notes.md."


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.fixture
def workspace(tmp_path):
    return make_workspace(tmp_path / "proj", modules=10)


# ── 1: the hybrid route is recognised, and is not capped ────────────────────


def test_hybrid_research_then_worker_route_surveys_freely_then_acts(
    workspace, isolated_streams
) -> None:
    """The hybrid route is a coding turn — and gets no discovery ceiling."""
    route = classify_user_request(HYBRID_REQUEST)
    assert route.lane is TaskLane.research, "precondition: not the implementation lane"
    assert route.action == "research_then_worker", (
        "precondition: this is the hybrid action, not answer-only research"
    )

    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        read_round("r2", 2),
        read_round("r3", 3),
        write_round(),
        final_round("Applied."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    recorder = Recorder()

    run(build_manager(workspace, HYBRID_REQUEST), recorder, route=route)

    reads = recorder.results_named("read_file")
    assert len(reads) == 4 and all(r.ok for r in reads), (
        "non-vacuous: four sequential discovery rounds really ran, successfully — "
        f"got {[(r.name, r.ok) for r in recorder.tool_results()]}"
    )
    assert backend.request_shapes() == [False] * 6, (
        "no ceiling: every round returned new evidence, so the turn reached its "
        f"edit on an ordinary request — got {backend.request_shapes()}"
    )
    writes = recorder.results_named("write_file")
    assert writes and writes[0].ok, "the edit really applied"


def test_answer_only_research_owes_no_action(workspace, isolated_streams) -> None:
    """The boundary: an answer-only turn has no act to serialize."""
    route = TaskRoute(TaskLane.research, "web_research", 0.9, "answer-only research")
    assert not route_bears_production_action(route)

    backend = ScriptedBackend([
        read_round("r0", 0), read_round("r1", 1),
        read_round("r2", 2), read_round("r3", 3),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

    run(build_manager(workspace, "What is the current Godot release?"),
        Recorder(), route=route)

    assert not backend.focused_calls()


def test_both_gates_read_the_one_shared_predicate() -> None:
    """The condition exists once, so the two gates cannot drift apart."""
    hybrid = TaskRoute(TaskLane.research, "research_then_worker", 0.9, "hybrid")
    guard = PreEditLoopGuard()
    guard.focused = True

    assert route_bears_production_action(hybrid)
    assert implementation_action_pending(
        mode="single", route=hybrid, guard=guard, read_only=False
    )
    assert should_enter_focused_action(
        mode="single",
        route=hybrid,
        guard=guard,
        task_completion_context=False,
        state=FocusedActionState(),
    )


def test_a_hybrid_turns_probe_does_not_complete_it_before_the_write() -> None:
    """The probe-completion fix still reaches hybrid coding turns."""
    state = _SendState(
        mode="single",
        research_policy=None,
        task_route=TaskRoute(
            TaskLane.research, "research_then_worker", 0.9, "hybrid"
        ),
    )
    assert not state.probes_complete_action()


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, True),
        ({"mode": "planner"}, False),
        ({"read_only": True}, False),
        ({"route": TaskRoute(TaskLane.research, "web_research", 0.9, "r")}, False),
        ({"route": None}, False),
        ({"guard": None}, False),
    ],
)
def test_the_predicate_reads_exactly_the_four_documented_facts(
    kwargs, expected
) -> None:
    base = dict(
        mode="single",
        route=TaskRoute(TaskLane.implementation, "implementation", 0.9, "unit"),
        guard=PreEditLoopGuard(),
        read_only=False,
    )
    base.update(kwargs)
    assert implementation_action_pending(**base) is expected


def test_an_applied_write_ends_the_pending_action() -> None:
    import json

    guard = PreEditLoopGuard()
    guard.observe_result("write_file", True, json.dumps({"applied": True}))
    assert not implementation_action_pending(
        mode="single",
        route=TaskRoute(TaskLane.implementation, "implementation", 0.9, "unit"),
        guard=guard,
        read_only=False,
    )


# ── 2: a missing route is resolved, not treated as "no route" ───────────────


def test_a_missing_route_is_resolved_from_the_user_message(
    workspace, isolated_streams
) -> None:
    """A caller that passes no route cannot silently change the turn's class.

    The route is resolved from the latest real user message — the same
    deterministic classification the send layer performs — so the turn behaves
    exactly as it would have had the caller passed one. It gets no ceiling
    either way.
    """
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        read_round("r2", 2),
        write_round(),
        final_round("Applied."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    recorder = Recorder()

    run(build_manager(workspace, CODING_REQUEST), recorder, route=None)

    reads = recorder.results_named("read_file")
    assert len(reads) == 3 and all(r.ok for r in reads), (
        "non-vacuous: three sequential discovery rounds really ran"
    )
    assert backend.request_shapes() == [False] * 5, (
        f"no ceiling on a resolved route either — got {backend.request_shapes()}"
    )
    writes = recorder.results_named("write_file")
    assert writes and writes[0].ok, "the edit really executed"
    assert (workspace / "notes.md").read_text(encoding="utf-8") == (
        "# Notes\n\nacted\n"
    )


def test_a_resolved_route_still_reaches_focused_action_when_discovery_stalls(
    workspace, isolated_streams
) -> None:
    """Resolution is not cosmetic: the resolved route really governs the turn."""
    backend = ScriptedBackend([
        _same_listing("s1"),
        _same_listing("s2", path="./"),
        write_round(),
        final_round("Applied."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    recorder = Recorder()

    run(build_manager(workspace, CODING_REQUEST), recorder, route=None)

    assert backend.request_shapes() == [False, False, True, False], (
        "the stalled round forces the focused act on a resolved route too — "
        f"got {backend.request_shapes()}"
    )
    writes = recorder.results_named("write_file")
    assert writes and writes[0].ok
