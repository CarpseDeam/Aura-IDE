"""Pre-edit loop guard protocol: no discovery budgets, a stall is the transition.

The old guard refused discovery by a call count: a cumulative spend of
``MAX_DISCOVERY_CALLS_BEFORE_FOCUS`` calls ended broad discovery with an
exhaustion rejection, and a small allowance remained after focus. Those
counters are gone.  Discovery is never refused by a count — a turn may survey
as long as every call returns genuinely new evidence — and the focused action
protocol is entered on the first round that stops producing evidence.

What is asserted here:

* broad discovery is never refused, however many unique calls a turn makes;
* a stalled round (tools ran, results were seen, no new evidence, no progress)
  is the single protocol transition into the focused action request;
* a stalled round that followed a distinct failure is a recovery round, not a
  push into mutation;
* the exact-repeat read rejection is the only structured rejection, and it
  stays recoverable and dormant once any write has applied;
* rereads justified by a failure, a stale-file notice, or pending edit-recovery
  state stay allowed;
* candidate tracking and the no-prose-inspection contract survive;
* no internal steering messages exist any more.
"""

from __future__ import annotations

import inspect
import json

import pytest

from aura.conversation import pre_edit_loop_guard as guard_module
from aura.conversation.pre_edit_loop_guard import (
    DUPLICATE_READ_REASON,
    NARROW_READ_TOOLS,
    PreEditLoopGuard,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def unique_read(guard: PreEditLoopGuard, index: int, *, tool: str = "read_file") -> None:
    """One accepted discovery call whose result is genuinely new evidence."""
    path = f"src/module_{index}.py"
    args = {"path": path}
    assert guard.check(tool, args) is None, f"call {index} was unexpectedly rejected"
    guard.record(tool, args)
    guard.observe_result(
        tool, True, json.dumps({"path": path, "content": f"unique body {index}"})
    )


def burn_discovery(guard: PreEditLoopGuard, count: int, *, start: int = 0) -> None:
    """Spend *count* discovery rounds, each productive and unique."""
    for i in range(start, start + count):
        guard.begin_round()
        unique_read(guard, i)
        guard.end_round()


def stall_round(guard: PreEditLoopGuard, *, which: int = 0) -> None:
    """One round that repeats already-seen evidence: the stalled transition."""
    guard.begin_round()
    guard.record("read_file", {"path": f"src/module_{which}.py"})
    guard.observe_result(
        "read_file",
        True,
        json.dumps({"path": f"src/module_{which}.py", "content": f"unique body {which}"}),
    )
    guard.end_round()


def focused_guard(evidence: int = 5) -> PreEditLoopGuard:
    """A guard that has concluded discovery is over: evidence, then a stall."""
    guard = PreEditLoopGuard()
    burn_discovery(guard, evidence)
    stall_round(guard, which=0)
    assert guard.focused is True
    return guard


def applied_write(guard: PreEditLoopGuard) -> None:
    guard.observe_result("write_file", True, json.dumps({"applied": True}))


# ── discovery is unbounded ──────────────────────────────────────────────────


class TestDiscoveryIsUnbounded:

    def test_every_unique_call_is_allowed(self) -> None:
        guard = PreEditLoopGuard()
        # Far past any old budget, every call still runs and none is refused.
        burn_discovery(guard, 40)

        assert guard.focused is False, "unique evidence never stalls"
        assert guard.check("glob", {"pattern": "**/*.py"}) is None
        assert guard.check("search_codebase", {"query": "anything"}) is None

    def test_new_evidence_prevents_the_stalled_round_transition(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, 40)

        assert guard.focused is False
        assert guard.blocked_calls == 0

    def test_read_task_context_is_unbounded_observation(self) -> None:
        guard = PreEditLoopGuard()
        for i in range(40):
            guard.begin_round()
            args = {"query": f"question {i}"}
            assert guard.check("read_task_context", args) is None
            guard.record("read_task_context", args)
            guard.observe_result(
                "read_task_context", True, json.dumps({"answer": i, "query": args["query"]})
            )
            guard.end_round()

        assert guard.focused is False
        assert guard.check("read_task_context", {"query": "one more"}) is None


# ── the stalled round is the transition ─────────────────────────────────────


class TestStalledRoundIsTheTransition:

    def test_one_stalled_round_sets_focused(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        unique_read(guard, 0)
        guard.end_round()
        assert guard.focused is False

        stall_round(guard, which=0)
        assert guard.focused is True

    def test_a_round_that_saw_no_results_does_not_fire(self) -> None:
        """Recording intent is not a stalled round: nobody has seen a result."""
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "a.py"})
        guard.end_round()

        assert guard.focused is False

    def test_a_round_with_no_tools_does_not_fire(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.end_round()

        assert guard.focused is False

    def test_focused_is_set_once_and_never_reset(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, 1)
        stall_round(guard, which=0)
        assert guard.focused is True

        # Later genuinely new evidence does not reopen ordinary discovery.
        guard.begin_round()
        unique_read(guard, 9)
        guard.end_round()
        assert guard.focused is True

    def test_identical_evidence_under_changed_arguments_still_stalls(self) -> None:
        """Cosmetic argument changes cannot launder the same evidence as new."""
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "src/module_0.py"})
        guard.observe_result(
            "read_file", True, json.dumps({"path": "src/module_0.py", "content": "b"})
        )
        guard.end_round()

        guard.begin_round()
        guard.record("read_file", {"path": "src/module_0.py", "_n": 1})
        guard.observe_result(
            "read_file", True, json.dumps({"path": "src/module_0.py", "content": "b"})
        )
        guard.end_round()

        assert guard.focused is True


# ── after focus: narrow reads, writes, commands ─────────────────────────────


class TestAfterFocus:

    def test_narrow_reads_of_known_files_remain_usable(self) -> None:
        guard = focused_guard()
        assert guard.is_known_candidate("src/module_3.py")

        assert guard.check(
            "read_file_range", {"path": "src/module_3.py", "start": 10, "end": 60}
        ) is None
        assert guard.check("read_file_outline", {"path": "src/module_3.py"}) is None
        # A fresh broad read of a file the turn has not opened is not a repeat
        # and is not gated by any count.
        assert guard.check("read_file", {"path": "src/unopened.py"}) is None

    def test_writes_are_never_gated_by_the_guard(self) -> None:
        guard = focused_guard()

        assert guard.check("write_file", {"path": "src/module_1.py"}) is None
        assert guard.check("patch_file", {"path": "src/module_1.py"}) is None

    def test_commands_are_never_gated_by_the_guard(self) -> None:
        guard = focused_guard()

        assert guard.check("run_terminal_command", {"command": "pytest -q"}) is None
        assert guard.check("run_diagnostic_command", {"command": "git status"}) is None


# ── the exact-repeat read rejection ─────────────────────────────────────────


class TestDuplicateReadRejection:

    def _guard_with_one_read(self) -> PreEditLoopGuard:
        guard = PreEditLoopGuard()
        guard.begin_round()
        unique_read(guard, 0)
        guard.end_round()
        return guard

    def test_an_exact_repeat_read_is_rejected(self) -> None:
        guard = self._guard_with_one_read()
        rejection = guard.check("read_file", {"path": "src/module_0.py"})

        assert rejection is not None
        assert rejection["reason"] == DUPLICATE_READ_REASON

    def test_the_rejection_is_structured_and_recoverable(self) -> None:
        guard = self._guard_with_one_read()
        rejection = guard.check("read_file", {"path": "src/module_0.py"})

        assert rejection["ok"] is False
        assert rejection["recoverable"] is True, "this must never end the turn"
        assert rejection["loop_guard"] is True
        assert rejection["tool"] == "read_file"
        assert rejection["previous_calls"] == 1

    def test_the_rejection_payload_serializes(self) -> None:
        """It travels to the model as a tool result, so it must be JSON."""
        guard = self._guard_with_one_read()
        rejection = guard.check("read_file", {"path": "src/module_0.py"})

        assert json.loads(json.dumps(rejection))["reason"] == DUPLICATE_READ_REASON

    def test_a_first_sight_of_the_same_file_is_not_rejected(self) -> None:
        guard = self._guard_with_one_read()
        assert guard.check("read_file", {"path": "src/module_1.py"}) is None

    @pytest.mark.parametrize("tool", sorted(NARROW_READ_TOOLS))
    def test_narrow_repeats_are_still_guarded(self, tool: str) -> None:
        """An exact repeat returns the same bytes whatever the tool."""
        guard = PreEditLoopGuard()
        args = {"path": "a.py"}
        guard.record(tool, args)

        assert guard.check(tool, args)["reason"] == DUPLICATE_READ_REASON


# ── recovery paths stay open ────────────────────────────────────────────────


class TestRecoveryPathsStayOpen:

    def test_a_tool_failure_buys_grace_for_a_reread(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.observe_result("patch_file", False, json.dumps({"error": "no match"}))
        guard.end_round()

        assert guard.check("read_file", {"path": "notes.md"}) is None

    def test_pending_edit_recovery_allows_a_reread(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        unique_read(guard, 0)
        guard.end_round()

        assert guard.check("read_file", {"path": "src/module_0.py"}) is not None
        assert guard.check(
            "read_file", {"path": "src/module_0.py"}, recovery_pending=True
        ) is None

    def test_a_stale_file_notice_clears_only_the_named_paths(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "src/module_0.py"})
        guard.record("read_file", {"path": "src/module_1.py"})
        guard.end_round()

        guard.note_stale_paths(["src/module_0.py"])
        assert guard.check("read_file", {"path": "src/module_0.py"}) is None
        assert guard.check("read_file", {"path": "src/module_1.py"}) is not None


# ── the first applied write ends the boundary ───────────────────────────────


class TestAppliedWriteDisablesTheBoundary:

    def test_an_applied_write_reopens_repeat_reads(self) -> None:
        guard = focused_guard()
        assert guard.check("read_file", {"path": "src/module_0.py"}) is not None

        applied_write(guard)

        assert guard.write_applied is True
        assert guard.check("read_file", {"path": "src/module_0.py"}) is None
        assert guard.check("glob", {"pattern": "**/*.py"}) is None

    def test_a_write_that_did_not_apply_does_not_disable_the_boundary(self) -> None:
        guard = focused_guard()
        guard.observe_result(
            "write_file", True, json.dumps({"applied": False, "error": "rejected"})
        )

        assert guard.write_applied is False
        assert guard.check("read_file", {"path": "src/module_0.py"}) is not None

    def test_discovery_after_a_write_is_untracked(self) -> None:
        guard = PreEditLoopGuard()
        applied_write(guard)
        for i in range(30):
            guard.record("read_file", {"path": f"after_{i}.py"})

        assert guard.focused is False
        assert guard.blocked_calls == 0


# ── mechanical, not semantic ────────────────────────────────────────────────


class TestMechanicalNotSemantic:

    def test_the_guard_never_inspects_model_prose(self) -> None:
        """check/record/observe_result take tool names, args, and payloads only."""
        for method in (
            PreEditLoopGuard.check,
            PreEditLoopGuard.record,
            PreEditLoopGuard.observe_result,
        ):
            params = set(inspect.signature(method).parameters)
            assert not params & {"content", "text", "message", "prompt", "task"}

    def test_no_replacement_manager_or_phase_machine_was_added(self) -> None:
        source = inspect.getsource(guard_module)
        for banned in ("class .*Manager", "class .*Workflow", "class .*Planner"):
            assert banned.replace(".*", "") not in source

        classes = [
            name
            for name, value in vars(guard_module).items()
            if inspect.isclass(value) and value.__module__ == guard_module.__name__
        ]
        assert classes == ["PreEditLoopGuard"], f"unexpected new owner: {classes}"


# ── candidate tracking ──────────────────────────────────────────────────────


class TestCandidateTracking:

    def test_paths_from_call_arguments_become_candidates(self) -> None:
        guard = PreEditLoopGuard()
        guard.record("read_files", {"paths": ["a/b.py", "c/d.py"]})

        assert guard.is_known_candidate("a/b.py")
        assert guard.is_known_candidate("c/d.py")

    def test_paths_found_in_search_results_become_candidates(self) -> None:
        guard = PreEditLoopGuard()
        guard.observe_result(
            "grep_search",
            True,
            json.dumps({"matches": [{"path": "found/here.py", "line": 3}]}),
        )

        assert guard.is_known_candidate("found/here.py")

    def test_windows_and_posix_separators_match(self) -> None:
        guard = PreEditLoopGuard()
        guard.record("read_file", {"path": r"aura\conversation\manager.py"})

        assert guard.is_known_candidate("aura/conversation/manager.py")

    def test_an_unknown_file_is_not_a_candidate(self) -> None:
        guard = PreEditLoopGuard()
        guard.record("read_file", {"path": "a/b.py"})

        assert guard.is_known_candidate("totally/unrelated.py") is False

    def test_candidate_tracking_is_bounded(self) -> None:
        guard = PreEditLoopGuard()
        guard.observe_result(
            "glob",
            True,
            json.dumps({"files": [f"f{i}.py" for i in range(10_000)]}),
        )

        assert len(guard.candidate_files) <= guard_module._MAX_CANDIDATE_FILES


# ── failure recovery holds the focused transition for exactly one round ─────


def _failing_command_round(guard: PreEditLoopGuard) -> None:
    guard.begin_round()
    guard.record("run_diagnostic_command", {"command": "pytest -q"})
    guard.observe_result(
        "run_diagnostic_command",
        False,
        json.dumps({"requested_command": "pytest -q", "failure_class": "boom"}),
    )
    guard.end_round()


class TestFailureRecoveryHoldsFocusForOneRound:

    def test_the_failing_round_itself_is_not_a_transition(self) -> None:
        """A failure explains the stall; the round after it is for fixing the
        failure, not for forcing a mutation."""
        guard = PreEditLoopGuard()
        burn_discovery(guard, 1)
        _failing_command_round(guard)

        assert guard.focused is False
        assert guard._failure_active is True

    def test_a_recovery_round_that_recovers_nothing_is_the_transition(
        self,
    ) -> None:
        """The granted recovery round repeats the same bytes: no progress, no
        new evidence, no new distinct failure.  Recovery closes there and that
        same stalled round becomes the focused transition — it must not stay
        latched open, suppressing focus for the rest of the turn."""
        guard = PreEditLoopGuard()
        burn_discovery(guard, 1)
        _failing_command_round(guard)

        stall_round(guard, which=0)

        assert guard.focused is True
        assert guard._failure_active is False

    def test_recovery_clears_the_block_and_new_evidence_never_fires_focus(
        self,
    ) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, 1)
        _failing_command_round(guard)

        # The corrected command succeeds inside the granted recovery round:
        # recovery closes and the turn is moving again, so focus never fires.
        guard.begin_round()
        guard.record("run_diagnostic_command", {"command": "pytest -q"})
        guard.observe_result(
            "run_diagnostic_command",
            True,
            json.dumps({"exit_code": 0, "command": "pytest -q"}),
        )
        guard.end_round()

        assert guard._failure_active is False
        assert guard._failure_pending is False
        assert guard.focused is False


# ── guard state and ownership ───────────────────────────────────────────────


def test_guard_state_is_plain_data() -> None:
    """The guard stays inspectable: no hidden services, just fields."""
    guard = PreEditLoopGuard()
    for field_name in (
        "focused",
        "candidate_files",
        "write_applied",
        "seen_reads",
        "seen_evidence",
        "blocked_calls",
        "seen_failures",
        "repeated_failures",
    ):
        assert hasattr(guard, field_name), field_name
    assert isinstance(guard.candidate_files, set)


def test_no_second_guard_owns_this_behaviour() -> None:
    """PreEditLoopGuard remains the single owner named in the send state."""
    from aura.conversation.manager_send_state import _SendState

    state = _SendState(mode="single", research_policy=None)
    assert isinstance(state.pre_edit_guard, PreEditLoopGuard)

    worker = _SendState(mode="worker", research_policy=None)
    assert worker.pre_edit_guard is None


def test_the_counter_api_is_gone() -> None:
    """The removed budget, steering, and message plumbing must not exist."""
    source = inspect.getsource(guard_module)
    for banned in (
        "MAX_DISCOVERY_CALLS",
        "MAX_STAGNANT_ROUNDS",
        "FAILURE_GRACE",
        "DISCOVERY_EXHAUSTED",
        "take_internal_messages",
        "take_focus_message",
        "take_steering_message",
        "stagnant_rounds",
        "discovery_calls",
    ):
        assert banned not in source, f"{banned} must not exist in the guard"
