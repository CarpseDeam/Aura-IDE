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

And the act that follows is not one swing at the task.  A focused mutation that
did not apply is *evidence*: its result goes back into the ordinary tool loop,
the model inspects, corrects, and acts again, and the focused request is handed
back every time the ordinary loop actually advances the turn.  Nothing counts
those corrections.  The turn ends without a write only when a round neither
advanced it nor produced a failure the guard had not already fingerprinted.

Every loop test here drives the real ``ConversationManager`` over the real
``ToolRegistry`` and a real workspace, and asserts the tools really executed.
"""

from __future__ import annotations

import json

import pytest

from aura.client import Done, Event
from aura.conversation.focused_action import (
    ACTION_FAILED_MESSAGE,
    FOCUSED_ACTION_THINKING,
    REPORT_BLOCKER,
    FocusedActionState,
    should_enter_focused_action,
)
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.tool_limits import MAX_TOOL_CALLS_BY_MODE, ToolLimitState
from aura.conversation.tools._types import ApprovalDecision
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

    def test_four_novel_rounds_through_the_real_loop_stay_ordinary(
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


# ── 4-5: a failed act is evidence, and the turn keeps working ───────────────


STALL = [
    tool_round([("d1", "list_directory", {"path": "."})]),
    tool_round([("d2", "list_directory", {"path": "./"})]),
]
"""Two rounds returning the same listing: discovery stalls, focus opens."""


def escaping_write(call_id: str = "bad") -> list[Event]:
    """A write that fails on its own terms: the path escapes the workspace."""
    return tool_round([(call_id, "write_file", {
        "path": "../outside.md", "content": "nope",
    })])


def missing_delete(call_id: str = "del") -> list[Event]:
    """A delete of a file that is not there — a *different* failure shape."""
    return tool_round([(call_id, "delete_file", {"path": "gone.md"})])


def stale_patch(call_id: str = "p1") -> list[Event]:
    """A patch whose ``old`` block is not in the file: a stale-patch failure."""
    return tool_round([(call_id, "patch_file", {
        "path": "notes.md",
        "edits": [{"old": "text that is not there", "new": "acted"}],
    })])


def good_patch(call_id: str = "p2") -> list[Event]:
    return tool_round([(call_id, "patch_file", {
        "path": "notes.md",
        "edits": [{"old": "old body", "new": "acted"}],
    })])


def read_notes(call_id: str) -> list[Event]:
    return tool_round([(call_id, "read_file", {"path": "notes.md"})])


class RejectFirstProposal:
    """Reject the first proposal outright; approve whatever Aura brings back.

    Stands in for a user who does not want *that* change. It never sets
    reject-all, so the turn is free to come back with a different approach.
    """

    def __init__(self) -> None:
        self.proposals: list[str] = []

    def __call__(self, request):
        self.proposals.append(str(getattr(request, "rel_path", "")))
        if len(self.proposals) == 1:
            return ApprovalDecision(action="reject")
        return ApprovalDecision(action="approve")


class TestAFailedActIsEvidenceNotCompletion:
    """A mutation that did not apply returns to the loop; only evidence ends it."""

    def test_a_repeated_failure_never_opens_recovery_in_the_guard(self) -> None:
        """At the guard: the same fingerprint is not a changed diagnosis."""
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
            "the same failure repeated is nothing the turn had not already seen"
        )
        assert guard.repeated_failures == 1

    def test_recovery_open_holds_the_focused_transition_for_a_round(self) -> None:
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
            "a failure the turn has not seen buys an ordinary round to read it"
        )

    def test_a_failed_focused_write_gathers_evidence_and_then_edits(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 2: focused write fails → new evidence → corrected write applies."""
        backend = ScriptedBackend([
            *STALL,
            escaping_write(),                     # focused act #1 — fails
            read_round("rec", 7),                 # ordinary: new evidence
            write_round("good"),                  # focused act #2 — applies
            final_round("Applied after correcting the path."),
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

        assert backend.request_shapes() == [
            False, False, True, False, True, False,
        ], (
            "stall → focused act → ordinary round → corrected focused act — "
            f"got {backend.request_shapes()}"
        )
        assert ACTION_FAILED_MESSAGE not in recorder.chat_text, (
            "a failed act is evidence, not a completed task"
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )

    def test_two_different_failures_still_reach_a_successful_third_write(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 3: two distinct failures, each followed by new evidence, edit."""
        backend = ScriptedBackend([
            *STALL,
            escaping_write(),                     # failure A: path escape
            read_round("e1", 7),                  # new evidence
            missing_delete(),                     # failure B: a different shape
            read_round("e2", 8),                  # new evidence
            write_round("good"),                  # the third act — applies
            final_round("Applied on the third approach."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        assert [r.ok for r in recorder.results_named("write_file")] == [False, True]
        deletes = recorder.results_named("delete_file")
        assert len(deletes) == 1 and not deletes[0].ok, (
            "non-vacuous: the second, differently-shaped act really ran and failed"
        )
        assert backend.request_shapes() == [
            False, False, True, False, True, False, True, False,
        ], (
            "two distinct failures, each read and corrected, still reach the "
            f"edit — got {backend.request_shapes()}"
        )
        assert ACTION_FAILED_MESSAGE not in recorder.chat_text
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )

    def test_a_stale_patch_rereads_and_then_patches_successfully(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 4: a stale hunk is a reread, not the end of the task."""
        backend = ScriptedBackend([
            *STALL,
            stale_patch(),                        # focused act #1 — hunk not found
            read_notes("rr"),                     # the reread: new evidence
            good_patch(),                         # focused act #2 — applies
            final_round("Patched after rereading."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        patches = recorder.results_named("patch_file")
        assert len(patches) == 2 and [p.ok for p in patches] == [False, True], (
            f"non-vacuous: both patches really ran — got {patches}"
        )
        assert "patch_hunk_not_found" in str(patches[0].result), (
            "the first patch failed as a genuine stale hunk"
        )
        assert recorder.results_named("read_file"), "the reread really happened"
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )
        assert ACTION_FAILED_MESSAGE not in recorder.chat_text

    def test_a_rejected_proposal_recovers_with_a_different_approved_one(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 5: the user's "no" is not the turn's failure.

        The rejected proposal is never silently re-sent: the second act carries
        materially different content, and it is the one that lands.
        """
        backend = ScriptedBackend([
            *STALL,
            write_round("w1", body="rejected body"),   # focused act #1 — rejected
            read_notes("rr"),                          # new evidence
            write_round("w2", body="approved body"),   # a different proposal
            final_round("Applied the revised change."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        approvals = RejectFirstProposal()

        run(build_manager(workspace), recorder, approval_cb=approvals)

        writes = recorder.results_named("write_file")
        assert len(writes) == 2 and [w.ok for w in writes] == [False, True], (
            f"non-vacuous: both proposals really reached approval — got {writes}"
        )
        assert "not_applied_user_rejected" in str(writes[0].result)
        assert approvals.proposals == ["notes.md", "notes.md"]
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\napproved body\n"
        ), "the approved, materially different proposal is what landed"
        assert ACTION_FAILED_MESSAGE not in recorder.chat_text

    def test_the_same_failure_with_nothing_new_ends_truthfully(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 6: a repeat of a known failure, knowing nothing new, ends."""
        backend = ScriptedBackend([
            *STALL,
            escaping_write("bad1"),               # focused act — distinct failure
            escaping_write("bad2"),               # ordinary round — the same failure
            # Anything past this point is the unbounded loop this forbids.
            read_round("x1", 8),
            read_round("x2", 9),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        writes = recorder.results_named("write_file")
        assert len(writes) == 2 and not any(w.ok for w in writes), (
            "non-vacuous: both writes really ran and really failed"
        )
        assert len(backend.calls) == 4, (
            "the repeat of a known failure ends the turn — "
            f"got {backend.request_shapes()}"
        )
        assert ACTION_FAILED_MESSAGE in recorder.chat_text
        assert isinstance(recorder.events[-1], Done), "the turn is terminated"

    def test_a_round_that_learns_nothing_after_an_act_ends_truthfully(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 7: no progress and no new evidence is the honest ending."""
        backend = ScriptedBackend([
            *STALL,
            escaping_write(),
            # The round after the act: a listing already seen. Nothing learned,
            # nothing failed anew — nothing left can change what happens next.
            tool_round([("d3", "list_directory", {"path": "."})]),
            read_round("x1", 8),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        assert recorder.results_named("write_file"), "the act really ran"
        assert len(backend.calls) == 4, (
            f"no request after the round that learned nothing — "
            f"{backend.request_shapes()}"
        )
        assert ACTION_FAILED_MESSAGE in recorder.chat_text
        assert isinstance(recorder.events[-1], Done)

    def test_a_user_rejection_alone_never_ends_the_turn(
        self, workspace, isolated_streams
    ) -> None:
        """A rejection is preserved as a result and handed back to the loop.

        With reject-all standing, the turn still gets to look again; what ends
        it is the *second* identical rejection, which teaches it nothing new —
        not the rejection itself.
        """
        backend = ScriptedBackend([
            *STALL,
            write_round("w1"),                    # focused act — rejected
            read_round("rec", 7),                 # new evidence: the loop continues
            write_round("w2", body="again"),      # a second act, rejected identically
            read_round("x1", 8),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder, approval_cb=reject_all)

        writes = recorder.results_named("write_file")
        assert len(writes) == 2 and not any(w.ok for w in writes), (
            "non-vacuous: the write tool really ran twice and was really rejected"
        )
        assert backend.request_shapes()[:5] == [False, False, True, False, True], (
            "the first rejection returned to the loop and reached a second act — "
            f"got {backend.request_shapes()}"
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nold body\n"
        ), "nothing was written"
        assert ACTION_FAILED_MESSAGE in recorder.chat_text
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

    def test_the_catastrophic_emergency_brake_is_untouched(self) -> None:
        """The 300-call backstop still fires, and it is the only hard stop."""
        limits = ToolLimitState(mode="single")
        assert MAX_TOOL_CALLS_BY_MODE["single"] == 300
        for _ in range(300):
            allowed, _info = limits.check("read_file")
            assert allowed, "the brake is a catastrophic backstop, not a budget"
            limits.record("read_file")
        allowed, info = limits.check("read_file")
        assert not allowed and info["limit_reached"] is True
        assert info["reason"] == "single_emergency_tool_call_limit_reached"

    def test_no_module_reintroduces_a_pre_write_budget(self) -> None:
        """The ceiling is gone and nothing counted or allowanced replaced it."""
        from pathlib import Path

        import aura.conversation.focused_action as fa
        import aura.conversation.manager as manager_mod
        import aura.conversation.manager_send_state as mss
        import aura.conversation.pre_edit_loop_guard as guard_mod

        for module in (fa, mss, guard_mod, manager_mod):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "ImplementationStage" not in source
            assert "implementation_staging_applies" not in source
            assert "FINAL_EVIDENCE" not in source
            # The one-recovery-per-turn allowance, and any successor to it.
            assert "recovery_used" not in source
            assert "open_recovery" not in source
            assert "awaiting_recovery" not in source

    def test_the_focused_state_owns_no_lifetime_allowance(self) -> None:
        """``FocusedActionState`` tracks the current request, and nothing else."""
        fields = set(FocusedActionState.__dataclass_fields__)
        assert fields == {
            "spent",
            "active",
            "blocked",
            "selected_thinking",
            "exposed_tools",
            "selected_action",
            "outcome",
            "contract_violated",
        }
        assert not any(
            "recovery" in name or "attempt" in name or "count" in name
            for name in fields
        )
