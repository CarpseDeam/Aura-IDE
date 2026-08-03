"""Discovery ends on evidence, never on a count.

The removed design gave a production implementation turn exactly two ordinary
requests before its first applied write.  It was brittle in the one way that
matters: a task whose edit target is only identifiable after reading something
found in the *second* hop reached focused action under-informed and produced a
blocker, purely because it had needed a third sequential request.  Needing three
requests is not a defect.  Circling is.

So the ceiling is gone, and nothing replaced it — no file, request, token, or
time budget anywhere before the first write.  What ends discovery is evidence
that the turn has stopped moving:

* a completed round that gathered no new evidence, ran no successful command,
  and applied no mutation;
* a short repeating cycle — ``A, B, A, B`` — across rounds that each look
  individually productive;
* an exact repeated observation call, still rejected outright.

And the act that follows is no longer all-or-nothing: a first *distinct* focused
mutation failure buys exactly one ordinary recovery round, through the same
failure fingerprints the guard already keeps, after which the turn either
succeeds or ends truthfully.  There is no second retry manager and no way to
cycle focus → failure → focus.

Every loop test here drives the real ``ConversationManager`` over the real
``ToolRegistry`` and a real workspace, and asserts the tools really executed.
"""

from __future__ import annotations

import json

import pytest

from aura.client import Done
from aura.conversation.focused_action import (
    ACTION_FAILED_MESSAGE,
    FOCUSED_ACTION_THINKING,
    REPORT_BLOCKER,
    FocusedActionState,
    should_enter_focused_action,
)
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    IMPLEMENTATION_ROUTE,
    Recorder,
    ScriptedBackend,
    build_manager,
    final_round,
    make_workspace,
    read_round,
    reject_all,
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
def workspace(tmp_path):
    return make_workspace(tmp_path / "proj")


# ── unit-level guard helpers ────────────────────────────────────────────────


def novel_read(guard: PreEditLoopGuard, index: int) -> None:
    """One round that reads a file never read before: genuinely new evidence."""
    guard.begin_round()
    args = {"path": f"mod_{index:02d}.py"}
    guard.record("read_file", args)
    guard.observe_result(
        "read_file", True, json.dumps({"path": args["path"], "content": f"# {index}"})
    )
    guard.end_round()


def repeat_round(guard: PreEditLoopGuard, name: str, args: dict, payload) -> None:
    """One round whose call and result are both exactly as given."""
    guard.begin_round()
    guard.record(name, args)
    guard.observe_result(name, True, json.dumps(payload))
    guard.end_round()


# ── 1: novelty is never bounded by a count ──────────────────────────────────


class TestNewEvidenceIsNeverBounded:

    def test_many_novel_rounds_never_force_focused_action(self) -> None:
        """Proof 1, at the guard: six sequential novel rounds, still no transition."""
        guard = PreEditLoopGuard()
        for index in range(6):
            novel_read(guard, index)
            assert not guard.focused, (
                f"round {index + 1} returned genuinely new evidence and must not "
                "end discovery"
            )
        assert guard.last_round_advanced

    def test_three_novel_rounds_through_the_real_loop_stay_ordinary(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 1, through the send loop: the removed two-hop ceiling is gone."""
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

        run(build_manager(workspace), recorder)

        reads = recorder.results_named("read_file")
        assert len(reads) == 4 and all(r.ok for r in reads), (
            "non-vacuous: four sequential discovery rounds really executed — "
            f"got {[(r.name, r.ok) for r in recorder.tool_results()]}"
        )
        assert backend.request_shapes() == [False] * 6, (
            "no focused action request: every round returned new evidence, so "
            f"discovery was never forced to end — got {backend.request_shapes()}"
        )
        writes = recorder.results_named("write_file")
        assert writes and writes[0].ok
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )


# ── 2: a stalled round still ends discovery ─────────────────────────────────


class TestAStalledRoundEndsDiscovery:

    def test_a_round_with_no_new_evidence_sets_focused(self) -> None:
        """Proof 2, at the guard."""
        guard = PreEditLoopGuard()
        payload = {"path": "a.py", "content": "same"}
        repeat_round(guard, "list_directory", {"path": "."}, payload)
        assert not guard.focused
        # A different call returning evidence already seen: nothing was learned.
        repeat_round(guard, "list_directory", {"path": "./"}, payload)
        assert guard.focused, "a round that gathered nothing ends discovery"
        assert not guard.last_round_advanced

    def test_a_stalled_round_forces_focused_action_through_the_loop(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 2, through the send loop."""
        backend = ScriptedBackend([
            tool_round([("d1", "list_directory", {"path": "."})]),
            # A cosmetically different call returning the identical listing.
            tool_round([("d2", "list_directory", {"path": "./"})]),
            write_round("w1"),
            final_round("Applied."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        listings = recorder.results_named("list_directory")
        assert len(listings) == 2 and all(r.ok for r in listings), (
            "non-vacuous: both listings really ran and really succeeded"
        )
        assert backend.request_shapes() == [False, False, True, False], (
            "the stalled second round forces the focused action request — "
            f"got {backend.request_shapes()}"
        )
        assert backend.focused_calls()[0]["thinking"] == FOCUSED_ACTION_THINKING


# ── 3: an A, B, A, B cycle ends discovery ───────────────────────────────────


class TestCycleDetection:

    def _cycle_round(self, guard: PreEditLoopGuard, which: str) -> None:
        if which == "A":
            guard.begin_round()
            guard.record("read_file", {"path": "a.py"})
            guard.observe_result(
                "read_file", True, json.dumps({"path": "a.py", "content": "A"})
            )
            guard.end_round()
        else:
            guard.begin_round()
            guard.record("run_diagnostic_command", {"command": "pytest -x"})
            guard.observe_result(
                "run_diagnostic_command",
                False,
                json.dumps({"command": "pytest -x", "exit_code": 1, "error": "boom"}),
            )
            guard.end_round()

    def test_an_a_b_a_b_cycle_sets_focused(self) -> None:
        """Proof 3: two alternating rounds, neither of which stalls on its own.

        ``A`` is a read whose evidence is only new the first time; ``B`` is a
        command failure that is only distinct the first time. Each round looks
        individually explicable, which is exactly why the stalled-round rule
        alone cannot see the cycle.
        """
        guard = PreEditLoopGuard()
        self._cycle_round(guard, "A")
        self._cycle_round(guard, "B")
        assert not guard.focused, "two rounds are not yet a cycle"
        self._cycle_round(guard, "A")
        self._cycle_round(guard, "B")
        assert guard.focused, "A, B, A, B is a cycle and ends discovery"
        assert guard.cycled, "the transition is attributed to the cycle rule"

    def test_alternating_but_progressing_rounds_are_not_a_cycle(self) -> None:
        """The boundary: different results each time is not a repeat."""
        guard = PreEditLoopGuard()
        for index in range(6):
            novel_read(guard, index)
        assert not guard.focused
        assert not guard.cycled

    def test_a_cycle_forces_focused_action_through_the_loop(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 3, through the send loop, where the stall rule cannot help.

        Both alternating rounds run a command that *succeeds*, which is forward
        progress by the stall rule's own reckoning — so it resets on every
        single round and can never fire here. The turn is still plainly
        circling, and the cycle rule is what sees it.
        """
        def cycle(call_id: str, command: str):
            return tool_round([(call_id, "run_diagnostic_command", {
                "command": command,
            })])

        backend = ScriptedBackend([
            cycle("a1", "python --version"),
            cycle("b1", "python -V"),
            cycle("a2", "python --version"),
            cycle("b2", "python -V"),
            write_round("w1"),
            final_round("Applied."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        commands = recorder.results_named("run_diagnostic_command")
        assert len(commands) == 4 and all(r.ok for r in commands), (
            "non-vacuous: all four commands really ran and really succeeded — "
            f"got {[(r.name, r.ok) for r in recorder.tool_results()]}"
        )
        assert backend.request_shapes() == [False, False, False, False, True, False], (
            "the repeating cycle ends discovery even though every round "
            f"succeeded — got {backend.request_shapes()}"
        )
        writes = recorder.results_named("write_file")
        assert writes and writes[0].ok


# ── 4-5: bounded focused-mutation recovery ──────────────────────────────────


class TestFocusedMutationRecovery:
    """The focused state is reusable exactly once, and only for recovery."""

    def test_a_first_distinct_failure_opens_one_recovery_round(self) -> None:
        state = FocusedActionState(spent=True)
        assert state.open_recovery(), "the first failure is granted recovery"
        assert not state.spent, "focused action is re-armed behind the round"
        assert state.awaiting_recovery

    def test_recovery_is_granted_at_most_once(self) -> None:
        state = FocusedActionState(spent=True)
        assert state.open_recovery()
        state.spent = True
        assert not state.open_recovery(), (
            "a second grant would be an unbounded focus/failure/recovery cycle"
        )
        assert state.spent, "the focused request stays spent"

    def test_a_repeated_failure_never_opens_recovery_in_the_guard(self) -> None:
        """Proof 5, at the guard: the same fingerprint buys nothing."""
        guard = PreEditLoopGuard()
        payload = json.dumps({"path": "notes.md", "error": "write rejected by user"})

        guard.begin_round()
        guard.record("write_file", {"path": "notes.md", "content": "x"})
        guard.observe_result("write_file", False, payload)
        guard.end_round()
        assert guard.recovery_open, "the first distinct failure opens recovery"

        guard.begin_round()
        guard.record("write_file", {"path": "notes.md", "content": "x"})
        guard.observe_result("write_file", False, payload)
        guard.end_round()
        assert not guard.recovery_open, (
            "the same failure repeated grants no further recovery"
        )
        assert guard.repeated_failures == 1

    def test_recovery_open_holds_the_focused_transition_for_one_round(self) -> None:
        guard = PreEditLoopGuard()
        guard.focused = True
        args = dict(
            mode="single",
            route=IMPLEMENTATION_ROUTE,
            guard=guard,
            task_completion_context=False,
            state=FocusedActionState(),
        )
        assert should_enter_focused_action(**args)

        guard.begin_round()
        guard.record("run_diagnostic_command", {"command": "pytest"})
        guard.observe_result(
            "run_diagnostic_command", False, json.dumps({"exit_code": 2})
        )
        guard.end_round()
        assert not should_enter_focused_action(**args), (
            "a distinct failure buys one ordinary round to correct it"
        )

    def test_a_failed_focused_write_recovers_and_then_edits(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 4, through the send loop.

        The focused write targets a path outside the workspace, so it fails on
        its own terms rather than by test scaffolding. That first distinct
        failure opens one ordinary recovery round, which gathers new evidence,
        and the re-armed focused request then edits successfully.
        """
        backend = ScriptedBackend([
            # Two rounds returning the same evidence: discovery stalls.
            tool_round([("d1", "list_directory", {"path": "."})]),
            tool_round([("d2", "list_directory", {"path": "./"})]),
            # Focused act #1 — fails: the path escapes the workspace.
            tool_round([("bad", "write_file", {
                "path": "../outside.md", "content": "nope",
            })]),
            # The one granted ordinary recovery round: new evidence.
            read_round("rec", 7),
            # Focused act #2 — the corrected write.
            write_round("good"),
            final_round("Applied after recovery."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        writes = recorder.results_named("write_file")
        assert len(writes) == 2, (
            f"non-vacuous: both write attempts really ran — got {writes}"
        )
        assert not writes[0].ok, "the first focused write really failed"
        assert writes[1].ok, "the corrected write really applied"

        shapes = backend.request_shapes()
        assert shapes[:5] == [False, False, True, False, True], (
            "stall → focused act → one ordinary recovery round → re-armed "
            f"focused act — got {shapes}"
        )
        assert ACTION_FAILED_MESSAGE not in recorder.chat_text, (
            "a recoverable first failure must not end the task"
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )

    def test_the_same_focused_failure_twice_ends_blocked(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 5, through the send loop: no second recovery, no loop."""
        bad_write = tool_round([("bad", "write_file", {
            "path": "../outside.md", "content": "nope",
        })])
        backend = ScriptedBackend([
            tool_round([("d1", "list_directory", {"path": "."})]),
            tool_round([("d2", "list_directory", {"path": "./"})]),
            bad_write,                                   # focused act #1
            read_round("rec", 7),                        # recovery round
            bad_write,                                   # focused act #2: same failure
            # Anything past this point is the unbounded loop this forbids.
            read_round("x1", 8),
            read_round("x2", 9),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        writes = recorder.results_named("write_file")
        assert len(writes) == 2 and not any(w.ok for w in writes), (
            "non-vacuous: both focused writes really ran and really failed"
        )
        assert len(backend.calls) == 5, (
            "no request after the second focused act — "
            f"got {backend.request_shapes()}"
        )
        assert ACTION_FAILED_MESSAGE in recorder.chat_text, (
            "the turn ends truthfully rather than circling"
        )
        assert isinstance(recorder.events[-1], Done), "the turn is terminated"

    def test_a_recovery_round_that_recovers_nothing_ends_blocked(
        self, workspace, isolated_streams
    ) -> None:
        """A granted round that learns nothing must not hand back the act."""
        backend = ScriptedBackend([
            tool_round([("d1", "list_directory", {"path": "."})]),
            tool_round([("d2", "list_directory", {"path": "./"})]),
            tool_round([("bad", "write_file", {
                "path": "../outside.md", "content": "nope",
            })]),
            # The granted recovery round: evidence already seen.
            tool_round([("d3", "list_directory", {"path": "."})]),
            read_round("x1", 8),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        assert len(backend.calls) == 4, (
            f"no request after the spent recovery round — {backend.request_shapes()}"
        )
        assert ACTION_FAILED_MESSAGE in recorder.chat_text
        assert isinstance(recorder.events[-1], Done)

    def test_a_rejected_focused_write_still_gets_its_one_recovery(
        self, workspace, isolated_streams
    ) -> None:
        """A user rejection is a distinct failure like any other, and bounded."""
        backend = ScriptedBackend([
            tool_round([("d1", "list_directory", {"path": "."})]),
            tool_round([("d2", "list_directory", {"path": "./"})]),
            write_round("w1"),
            read_round("rec", 7),
            write_round("w2", body="again"),
            read_round("x1", 8),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder, approval_cb=reject_all)

        writes = recorder.results_named("write_file")
        assert writes and not any(w.ok for w in writes), (
            "non-vacuous: the write tool really ran and was really rejected"
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nold body\n"
        ), "nothing was written"
        assert ACTION_FAILED_MESSAGE in recorder.chat_text
        assert len(backend.calls) <= 5, (
            f"recovery is bounded, not a loop — {backend.request_shapes()}"
        )
        assert isinstance(recorder.events[-1], Done)


# ── the surviving invariants ────────────────────────────────────────────────


class TestSurvivingInvariants:

    def test_an_exact_repeated_observation_is_still_rejected(self) -> None:
        guard = PreEditLoopGuard()
        args = {"path": "a.py"}
        guard.record("read_file", args)
        rejection = guard.check("read_file", args)
        assert rejection is not None and rejection["recoverable"]

    def test_a_blocked_state_never_re_enters_focused_action(self) -> None:
        guard = PreEditLoopGuard()
        guard.focused = True
        assert not should_enter_focused_action(
            mode="single",
            route=IMPLEMENTATION_ROUTE,
            guard=guard,
            task_completion_context=False,
            state=FocusedActionState(blocked=True),
        )

    def test_the_focused_tool_surface_is_still_mutations_plus_the_blocker(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend([
            tool_round([("d1", "list_directory", {"path": "."})]),
            tool_round([("d2", "list_directory", {"path": "./"})]),
            write_round("w1"),
            final_round("Applied."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        focused_call = backend.focused_calls()[0]
        names = {
            str(t.get("function", {}).get("name", ""))
            for t in focused_call.get("tools") or []
        }
        assert REPORT_BLOCKER in names
        assert "read_file" not in names and "grep_search" not in names
        assert focused_call.get("require_tool_call") is True
        assert focused_call["thinking"] == FOCUSED_ACTION_THINKING

    def test_no_module_reintroduces_a_pre_write_budget(self) -> None:
        """The ceiling is gone and nothing counted replaced it."""
        from pathlib import Path

        import aura.conversation.focused_action as fa
        import aura.conversation.manager_send_state as mss
        import aura.conversation.pre_edit_loop_guard as guard_mod

        for module in (fa, mss, guard_mod):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "ImplementationStage" not in source
            assert "implementation_staging_applies" not in source
            assert "FINAL_EVIDENCE" not in source
