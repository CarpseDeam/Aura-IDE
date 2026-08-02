"""Bounded pre-edit discovery.

The stagnation guard only fires when discovery stops producing evidence. But a
turn can burn a whole context window while *every* call returns something
genuinely new — a wide repository always has one more file to open. So accepted
discovery calls made before the first applied write are counted, and the count
alone drives the boundary.

What is asserted here:

* cumulative unique discovery triggers the focus instruction even though every
  single call returned new evidence;
* the focus instruction is internal steering, emitted exactly once;
* a small targeted allowance remains after focus;
* continued broad discovery is then rejected structurally and recoverably;
* narrow reads, ranges, and outlines of known candidates remain available;
* failure grace, stale-file rereads, and edit-recovery rereads remain available;
* the first applied write makes the whole boundary dormant;
* ``read_task_context`` is inside the budget;
* the boundary is a mechanical spend budget, not a task-complexity classifier.
"""

from __future__ import annotations

import inspect
import json

import pytest

from aura.conversation import pre_edit_loop_guard as guard_module
from aura.conversation.pre_edit_loop_guard import (
    DISCOVERY_EXHAUSTED_REASON,
    DISCOVERY_TOOLS,
    DUPLICATE_READ_REASON,
    MAX_DISCOVERY_CALLS_AFTER_FOCUS,
    MAX_DISCOVERY_CALLS_BEFORE_FOCUS,
    NARROW_READ_TOOLS,
    PreEditLoopGuard,
)

BEFORE = MAX_DISCOVERY_CALLS_BEFORE_FOCUS
AFTER = MAX_DISCOVERY_CALLS_AFTER_FOCUS


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
    """Spend *count* discovery calls, each in its own round, each productive."""
    for i in range(start, start + count):
        guard.begin_round()
        unique_read(guard, i)
        guard.end_round()


def applied_write(guard: PreEditLoopGuard) -> None:
    guard.observe_result("write_file", True, json.dumps({"applied": True}))


# ── cumulative discovery, despite continually new evidence ──────────────────


class TestCumulativeDiscoveryTriggersFocus:

    def test_normal_early_discovery_is_allowed(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE - 1)

        assert guard.take_internal_messages() == []
        assert guard.focused is False
        assert guard.check("glob", {"pattern": "**/*.py"}) is None

    def test_focus_fires_even_though_every_call_produced_new_evidence(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE)

        # Precisely the case the stagnation guard cannot see.
        assert guard.stagnant_rounds == 0, "no round was stagnant"
        assert len(guard.seen_evidence) == BEFORE, "every call returned new evidence"

        messages = guard.take_internal_messages()
        assert len(messages) == 1
        assert "discovery calls" in messages[0]
        assert guard.focused is True

    def test_the_focus_instruction_is_emitted_exactly_once(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE)

        assert guard.take_internal_messages() != []
        for _ in range(5):
            assert guard.take_focus_message() == ""

    def test_focus_names_the_way_forward(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE)
        message = guard.take_focus_message()

        assert "read_file_range" in message
        assert "read_file_outline" in message

    def test_read_task_context_counts_toward_the_budget(self) -> None:
        assert "read_task_context" in DISCOVERY_TOOLS

        guard = PreEditLoopGuard()
        for i in range(BEFORE):
            guard.begin_round()
            args = {"query": f"question {i}"}
            assert guard.check("read_task_context", args) is None
            guard.record("read_task_context", args)
            guard.end_round()

        assert guard.discovery_calls == BEFORE
        assert guard.take_focus_message() != ""

    @pytest.mark.parametrize(
        "tool",
        ["read_file", "read_files", "glob", "grep_search", "search_codebase",
         "list_directory", "find_usages", "read_task_context"],
    )
    def test_the_obvious_source_discovery_tools_are_all_counted(self, tool: str) -> None:
        guard = PreEditLoopGuard()
        guard.record(tool, {"path": "a.py", "pattern": "x", "query": "q"})

        assert guard.discovery_calls == 1, f"{tool} escaped the budget"

    @pytest.mark.parametrize("tool", sorted(NARROW_READ_TOOLS))
    def test_narrow_reads_are_not_counted(self, tool: str) -> None:
        guard = PreEditLoopGuard()
        guard.record(tool, {"path": "a.py"})

        assert guard.discovery_calls == 0, f"{tool} should not spend the budget"


# ── the targeted allowance after focus ──────────────────────────────────────


class TestPostFocusAllowance:

    def test_a_small_broad_allowance_remains_after_focus(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE)
        guard.take_internal_messages()

        assert guard.discovery_limit == BEFORE + AFTER
        for i in range(AFTER):
            guard.begin_round()
            unique_read(guard, BEFORE + i)
            guard.end_round()

        assert guard.discovery_calls == BEFORE + AFTER

    def test_targeted_reads_of_known_files_remain_usable_after_focus(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE)
        guard.take_internal_messages()

        assert guard.is_known_candidate("src/module_3.py")
        assert guard.check(
            "read_file_range", {"path": "src/module_3.py", "start": 10, "end": 60}
        ) is None
        assert guard.check("read_file_outline", {"path": "src/module_3.py"}) is None

    def test_targeted_reads_remain_usable_after_the_allowance_is_spent(self) -> None:
        guard = _exhausted_guard()

        assert guard.discovery_exhausted is True
        assert guard.check(
            "read_file_range", {"path": "src/module_1.py", "start": 1, "end": 40}
        ) is None
        assert guard.check("read_file_outline", {"path": "src/module_1.py"}) is None

    def test_writes_are_never_gated_by_this_boundary(self) -> None:
        guard = _exhausted_guard()

        assert guard.check("write_file", {"path": "src/module_1.py"}) is None
        assert guard.check("patch_file", {"path": "src/module_1.py"}) is None


# ── the structured rejection ────────────────────────────────────────────────


def _exhausted_guard() -> PreEditLoopGuard:
    guard = PreEditLoopGuard()
    burn_discovery(guard, BEFORE)
    guard.take_internal_messages()
    burn_discovery(guard, AFTER, start=BEFORE)
    return guard


class TestContinuedBroadDiscoveryIsRejected:

    def test_broad_discovery_is_rejected_once_the_budget_is_spent(self) -> None:
        guard = _exhausted_guard()
        rejection = guard.check("glob", {"pattern": "**/*.py"})

        assert rejection is not None
        assert rejection["reason"] == DISCOVERY_EXHAUSTED_REASON

    def test_the_rejection_is_structured_and_recoverable(self) -> None:
        guard = _exhausted_guard()
        rejection = guard.check("search_codebase", {"query": "retry cap"})

        assert rejection["ok"] is False
        assert rejection["recoverable"] is True, "this must never end the turn"
        assert rejection["loop_guard"] is True
        assert rejection["tool"] == "search_codebase"
        assert rejection["discovery_calls"] == BEFORE + AFTER
        assert rejection["discovery_limit"] == BEFORE + AFTER

    def test_the_rejection_names_what_is_still_available(self) -> None:
        guard = _exhausted_guard()
        rejection = guard.check("glob", {"pattern": "**/*.py"})

        assert set(rejection["still_available"]) == {
            "read_file_range", "read_file_outline", "write_file", "patch_file",
        }
        assert "src/module_1.py" in rejection["known_candidate_files"]
        assert "read_file_range" in rejection["message"]

    def test_the_rejection_payload_serializes(self) -> None:
        """It travels to the model as a tool result, so it must be JSON."""
        guard = _exhausted_guard()
        rejection = guard.check("glob", {"pattern": "**/*.py"})

        assert json.loads(json.dumps(rejection))["reason"] == DISCOVERY_EXHAUSTED_REASON

    @pytest.mark.parametrize("tool", sorted(DISCOVERY_TOOLS))
    def test_every_discovery_tool_is_refused_once_exhausted(self, tool: str) -> None:
        guard = _exhausted_guard()
        rejection = guard.check(tool, {"path": "brand/new.py", "query": "q", "pattern": "p"})

        assert rejection is not None
        assert rejection["reason"] == DISCOVERY_EXHAUSTED_REASON


# ── recovery paths stay open ────────────────────────────────────────────────


class TestRecoveryPathsStayOpen:

    def test_a_tool_failure_buys_grace_for_a_reread(self) -> None:
        guard = _exhausted_guard()
        guard.note_failure()

        assert guard.check("glob", {"pattern": "**/*.py"}) is None

    def test_pending_edit_recovery_allows_a_reread(self) -> None:
        guard = _exhausted_guard()

        assert guard.check(
            "read_file", {"path": "src/module_1.py"}, recovery_pending=True
        ) is None

    def test_a_stale_file_notice_clears_that_path(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        unique_read(guard, 0)
        guard.end_round()

        repeat = guard.check("read_file", {"path": "src/module_0.py"})
        assert repeat["reason"] == DUPLICATE_READ_REASON

        guard.note_stale_paths(["src/module_0.py"])
        assert guard.check("read_file", {"path": "src/module_0.py"}) is None

    def test_duplicate_detection_still_applies_to_narrow_reads(self) -> None:
        """An exact repeat returns the same bytes whatever the tool."""
        guard = PreEditLoopGuard()
        args = {"path": "src/module_1.py", "start": 1, "end": 40}
        guard.record("read_file_range", args)

        assert guard.check("read_file_range", args)["reason"] == DUPLICATE_READ_REASON


# ── the first applied write ends the boundary ───────────────────────────────


class TestAppliedWriteDisablesTheBoundary:

    def test_an_applied_write_reopens_broad_discovery(self) -> None:
        guard = _exhausted_guard()
        assert guard.check("glob", {"pattern": "**/*.py"}) is not None

        applied_write(guard)

        assert guard.write_applied is True
        assert guard.check("glob", {"pattern": "**/*.py"}) is None
        assert guard.check("search_codebase", {"query": "anything"}) is None

    def test_an_applied_write_stops_the_focus_instruction(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE)
        applied_write(guard)

        assert guard.take_internal_messages() == []

    def test_a_write_that_did_not_apply_does_not_disable_the_boundary(self) -> None:
        guard = _exhausted_guard()
        guard.observe_result(
            "write_file", True, json.dumps({"applied": False, "error": "rejected"})
        )

        assert guard.write_applied is False
        assert guard.check("glob", {"pattern": "**/*.py"}) is not None

    def test_discovery_after_a_write_is_no_longer_counted(self) -> None:
        guard = PreEditLoopGuard()
        applied_write(guard)
        for i in range(BEFORE * 3):
            guard.record("read_file", {"path": f"after_{i}.py"})

        assert guard.discovery_calls == 0
        assert guard.discovery_exhausted is False


# ── this is a spend budget, not a task classifier ───────────────────────────


class TestBudgetIsMechanicalNotSemantic:

    def test_the_limits_are_named_configurable_constants(self) -> None:
        assert isinstance(MAX_DISCOVERY_CALLS_BEFORE_FOCUS, int)
        assert isinstance(MAX_DISCOVERY_CALLS_AFTER_FOCUS, int)
        assert MAX_DISCOVERY_CALLS_BEFORE_FOCUS > 0
        assert MAX_DISCOVERY_CALLS_AFTER_FOCUS > 0

    def test_the_boundary_depends_only_on_call_count_not_content(self) -> None:
        """Two turns with wildly different subject matter behave identically."""
        trivial = PreEditLoopGuard()
        for i in range(BEFORE):
            trivial.record("read_file", {"path": f"typo_fix_{i}.txt"})

        sprawling = PreEditLoopGuard()
        for i in range(BEFORE):
            sprawling.record(
                "read_file",
                {"path": f"distributed_consensus_rewrite_{i}.py"},
            )

        assert trivial.discovery_calls == sprawling.discovery_calls
        assert bool(trivial.take_focus_message()) == bool(
            sprawling.take_focus_message()
        )

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


# ── the stagnation guard is untouched ───────────────────────────────────────


class TestStagnationGuardStillWorks:

    def test_stagnant_rounds_still_steer(self) -> None:
        guard = PreEditLoopGuard()
        # The first round's payload is new evidence; only the rounds after it
        # are stagnant, so one extra round is needed to reach the threshold.
        for _ in range(guard_module.MAX_STAGNANT_ROUNDS_BEFORE_STEER + 1):
            guard.begin_round()
            args = {"path": "same.py"}
            guard.record("read_file", args)
            guard.observe_result("read_file", True, json.dumps({"same": "payload"}))
            guard.end_round()

        assert guard.stagnant_rounds >= guard_module.MAX_STAGNANT_ROUNDS_BEFORE_STEER
        messages = guard.take_internal_messages()
        assert any("no new evidence" in m for m in messages)

    def test_both_instructions_can_arrive_together_in_order(self) -> None:
        guard = PreEditLoopGuard()
        burn_discovery(guard, BEFORE)
        # Now stall as well.
        for _ in range(guard_module.MAX_STAGNANT_ROUNDS_BEFORE_STEER):
            guard.begin_round()
            guard.record("read_file", {"path": "src/module_0.py"})
            guard.end_round()

        messages = guard.take_internal_messages()
        assert len(messages) == 2
        assert "discovery calls" in messages[0]
        assert "no new evidence" in messages[1]


def test_guard_state_is_plain_data() -> None:
    """The guard stays inspectable: no hidden services, just fields."""
    guard = PreEditLoopGuard()
    for field_name in (
        "discovery_calls", "focused", "candidate_files", "write_applied",
        "stagnant_rounds", "seen_reads", "seen_evidence", "blocked_calls",
    ):
        assert hasattr(guard, field_name), field_name
    assert isinstance(guard.discovery_calls, int)
    assert isinstance(guard.candidate_files, set)


def test_no_second_guard_owns_this_behaviour() -> None:
    """PreEditLoopGuard remains the single owner named in the send state."""
    from aura.conversation.manager_send_state import _SendState

    state = _SendState(mode="single", research_policy=None)
    assert isinstance(state.pre_edit_guard, PreEditLoopGuard)

    worker = _SendState(mode="worker", research_policy=None)
    assert worker.pre_edit_guard is None


def test_send_loop_appends_guard_messages_as_internal_only() -> None:
    """Steering must never look like a real user turn."""
    from aura.conversation.manager import ConversationManager

    source = inspect.getsource(ConversationManager.send)
    assert "take_internal_messages()" in source
    assert "append_internal_user_text(steering)" in source
    assert "append_user_text(steering)" not in source


def test_guard_messages_are_marked_aura_internal() -> None:
    from aura.conversation.history import History

    guard = PreEditLoopGuard()
    burn_discovery(guard, BEFORE)

    history = History()
    history.set_system("s")
    history.append_user_text("real user turn")
    for message in guard.take_internal_messages():
        history.append_internal_user_text(message)

    internal = [m for m in history.messages if m.get("aura_internal")]
    assert internal, "the focus instruction was not injected"
    assert all(m["role"] == "user" for m in internal)

    real_user_turns = [
        m for m in history.messages
        if m.get("role") == "user" and not m.get("aura_internal")
    ]
    assert len(real_user_turns) == 1, "internal steering redefined the user turn"
