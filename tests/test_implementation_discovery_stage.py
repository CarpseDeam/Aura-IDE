"""The implementation discovery stage: two ordinary hops, then act.

``PreEditLoopGuard`` bounds *repetition* — the same read twice, a round that
gathered nothing.  It deliberately never bounds *novelty*, and that is the loop
this stage closes: a production implementation turn whose every result is
technically new (a different file each round, a search with new matches, a fresh
diagnostic) is by the guard's own reckoning always still progressing, so
``guard.focused`` never fires and the send loop keeps opening another ordinary
reasoning stream.  Forever.

:class:`~aura.conversation.manager_send_state.ImplementationStage` is the second,
independent authority over the same transition::

    focused = stalled_round OR implementation_stage_exhausted

An implementation turn gets exactly two ordinary discovery hops before its first
applied write.  Inside a hop, batching is unlimited.  Across hops, there are two.

**What consumes a hop.** One thing: a structurally valid tool-call round in
which at least one tool actually executed.  Not whether it succeeded — a hop that
ran and failed still spent the model's chance to look.  Nothing that failed to
execute consumes anything.

**The accepted tradeoff**, asserted here rather than left implicit: a task that
truly needs a *third sequential* hop reaches focused action under-informed and
produces a visible structured blocker.  A blocker the user can see and answer is
the intended outcome; a turn that circles silently is not.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aura.client import (
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
)
from aura.conversation.focused_action import (
    FINAL_EVIDENCE_INSTRUCTION,
    FOCUSED_ACTION_THINKING,
    REPORT_BLOCKER,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.manager_send_state import (
    ImplementationStage,
    _SendState,
    implementation_staging_applies,
)
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry

IMPLEMENTATION_ROUTE = TaskRoute(
    lane=TaskLane.implementation,
    action="implementation",
    confidence=0.85,
    reason="scripted implementation turn",
)

RESEARCH_ROUTE = TaskRoute(
    lane=TaskLane.research,
    action="research",
    confidence=0.85,
    reason="scripted research turn",
)

#: The user's selection for these turns. Deliberately not "off", so the focused
#: request's thinking-off can be told apart from the ambient setting.
SELECTED_THINKING = "high"


# ── scripted production backend ─────────────────────────────────────────────


def tool_round(calls: list[tuple[str, str, dict]], *, text: str = "") -> list[Event]:
    """One streamed round that ends in tool calls."""
    events: list[Event] = []
    if text:
        events.append(ContentDelta(text=text))
    tool_calls = []
    for index, (call_id, name, args) in enumerate(calls):
        arguments = json.dumps(args)
        events.append(ToolCallStart(index=index, id=call_id, name=name))
        events.append(ToolCallArgsDelta(index=index, args_chunk=arguments))
        events.append(ToolCallEnd(index=index))
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    events.append(Done(
        finish_reason="tool_calls",
        full_message={
            "role": "assistant",
            "content": text,
            "tool_calls": tool_calls,
        },
    ))
    return events


def final_round(text: str) -> list[Event]:
    return [
        ContentDelta(text=text),
        Done(finish_reason="stop", full_message={"role": "assistant", "content": text}),
    ]


class ScriptedBackend:
    def __init__(self, rounds: list[list[Event]]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._rounds):
            return iter(self._rounds[index])
        return iter(final_round("(script exhausted)"))

    def ordinary_calls(self) -> list[dict]:
        return [c for c in self.calls if not c.get("require_tool_call")]

    def focused_calls(self) -> list[dict]:
        return [c for c in self.calls if c.get("require_tool_call")]


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    def of_type(self, kind: type) -> list[Event]:
        return [e for e in self.events if isinstance(e, kind)]

    @property
    def chat_text(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, ContentDelta))

    @property
    def reasoning_text(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, ReasoningDelta))


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(50):
        (root / f"mod_{i:02d}.py").write_text(
            f"# module {i}\n\nvalue = {i}\n", encoding="utf-8"
        )
    (root / "notes.md").write_text("# Notes\n\nold body\n", encoding="utf-8")
    return root


def approve_all(_request) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def build_manager(
    workspace: Path, user_text: str = "Update notes.md.", *, read_only: bool = False
) -> ConversationManager:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text(user_text)
    registry = ToolRegistry(
        workspace_root=workspace, mode="single", read_only=read_only
    )
    return ConversationManager(history, registry)


def run(
    manager: ConversationManager,
    recorder: Recorder,
    *,
    route: TaskRoute | None = IMPLEMENTATION_ROUTE,
    max_tool_rounds: int = 12,
) -> None:
    manager.send(
        on_event=recorder,
        approval_cb=approve_all,
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking=SELECTED_THINKING,
        hook_name=PRODUCTION_STREAM_HOOK,
        max_tool_rounds=max_tool_rounds,
        task_route=route,
    )


def read_round(call_id: str, index: int) -> list[Event]:
    return tool_round([(call_id, "read_file", {"path": f"mod_{index:02d}.py"})])


def system_texts(call: dict) -> list[str]:
    return [
        str(m.get("content", ""))
        for m in call.get("messages") or []
        if m.get("role") == "system"
    ]


# ── 1-3: two hops, whatever the evidence ────────────────────────────────────


class TestTwoOrdinaryHops:

    def test_first_executed_round_advances_to_the_final_request(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 1: one novel non-writing round → the final ordinary request."""
        backend = ScriptedBackend([read_round("r0", 0), read_round("r1", 1)])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        assert not backend.calls[1].get("require_tool_call"), (
            "the second request is still ordinary"
        )
        assert FINAL_EVIDENCE_INSTRUCTION in system_texts(backend.calls[1]), (
            "the second request carries the final-evidence protocol instruction"
        )

    def test_second_executed_round_forces_focused_action(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 2: two novel non-writing rounds → action-only, novelty or not."""
        backend = ScriptedBackend(
            [read_round("r0", 0), read_round("r1", 1), read_round("r2", 2)]
        )
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        assert backend.calls[2].get("require_tool_call") is True
        assert len(backend.ordinary_calls()) == 2, (
            "exactly two ordinary discovery requests, however novel the evidence"
        )

    def test_forty_unique_reads_cannot_buy_a_third_request(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 3: 40 unique calls spread over the two hops, still no third."""
        first = tool_round([
            (f"a{i}", "read_file", {"path": f"mod_{i:02d}.py"}) for i in range(20)
        ])
        second = tool_round([
            (f"b{i}", "read_file", {"path": f"mod_{i:02d}.py"}) for i in range(20, 40)
        ])
        backend = ScriptedBackend([first, second, read_round("c0", 45)])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        # Every one of the forty reads really ran — nothing was refused by a count.
        started = [e for e in recorder.of_type(ToolCallStart)]
        assert len(started) >= 40, f"expected 40 executed reads, saw {len(started)}"
        assert len(backend.ordinary_calls()) == 2
        assert backend.calls[2].get("require_tool_call") is True

    def test_batching_inside_a_hop_is_unlimited(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 4: a hop may carry any number of calls; it is still one hop."""
        big = tool_round([
            (f"m{i}", "read_file", {"path": f"mod_{i:02d}.py"}) for i in range(30)
        ])
        backend = ScriptedBackend([big, read_round("r1", 40)])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        assert not backend.calls[1].get("require_tool_call"), (
            "a 30-call batch consumes one hop, not thirty"
        )


# ── 5: probes are not an escape hatch ───────────────────────────────────────


class TestProbesDoNotExtendDiscovery:

    def test_successful_probe_cannot_keep_ordinary_discovery_open(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 5: a succeeding command is guard-progress but not a third hop.

        This is the sharpest case: a successful command sets the guard's
        ``_round_made_progress``, so the guard will *never* stall. Only the stage
        ends the turn.
        """
        probe = tool_round([
            ("p0", "run_diagnostic_command", {"command": "python --version"}),
        ])
        backend = ScriptedBackend([probe, read_round("r1", 1), read_round("r2", 2)])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        assert len(backend.ordinary_calls()) == 2
        assert backend.calls[2].get("require_tool_call") is True

    def test_a_probe_cannot_reopen_discovery_through_completion_context(
        self, workspace, isolated_streams
    ) -> None:
        """The second unbounded loop, closed.

        A *successful* call in ``ACTION_COMPLETION_TOOL_NAMES`` — ``git_status``,
        ``run_diagnostic_command``, the terminal tools — sets
        ``task_completion_context``, which used to suppress the focused
        transition for the rest of the turn. Before any write has applied that
        claim is false: nothing was completed. One probe followed by endless
        novel reads therefore ran forever. An exhausted stage now outranks a
        completion context no applied write ever earned.
        """
        # Must be a probe that genuinely *succeeds*: a failing one completes
        # nothing under any rule, and would make this test pass vacuously.
        # ``git_status`` fails outside a repository, so it is the wrong choice.
        rounds = [tool_round([
            ("p0", "run_diagnostic_command", {"command": "python --version"}),
        ])]
        rounds += [read_round(f"r{i}", i) for i in range(1, 20)]
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder(), max_tool_rounds=20)

        assert backend.focused_calls(), (
            "a probe must not buy an unbounded pre-write discovery loop"
        )
        assert len(backend.ordinary_calls()) == 2


# ── 6: an applied write leaves the protocol ─────────────────────────────────


class TestAppliedWriteBypassesFocusedAction:

    @pytest.mark.parametrize("stage_index", [0, 1])
    def test_write_in_either_hop_never_reaches_focused_action(
        self, workspace, isolated_streams, stage_index: int
    ) -> None:
        """Proof 6: the write path is untouched, in the first hop or the second."""
        write = tool_round([("w1", "write_file", {
            "path": "notes.md", "content": "# Notes\n\nnew body\n",
        })])
        rounds: list[list[Event]] = []
        if stage_index == 1:
            rounds.append(read_round("r0", 0))
        rounds.append(write)
        rounds.append(final_round("Applied the change."))
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        assert not backend.focused_calls(), (
            "an applied write leaves the pre-write protocol entirely"
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nnew body\n"
        )


# ── 7 + the failure rule: what does and does not consume a hop ──────────────


class TestWhatConsumesAHop:

    def test_preflight_rejected_batch_consumes_nothing(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 7: a batch rejected before execution ran nothing, so costs nothing."""
        # ``not_a_real_tool`` is not exposed: the whole batch is rejected before
        # any call executes.
        rejected = tool_round([("x0", "not_a_real_tool", {"q": 1})])
        backend = ScriptedBackend([
            rejected, read_round("r1", 1), read_round("r2", 2), read_round("r3", 3),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        assert len(backend.ordinary_calls()) == 3, (
            "the rejected batch did not spend a hop, so two real hops remained"
        )
        assert backend.calls[3].get("require_tool_call") is True

    def test_an_executed_round_whose_tools_failed_still_consumes_a_hop(
        self, workspace, isolated_streams
    ) -> None:
        """The settled failure rule, as a regression test.

        A round whose tools *ran and failed* spent the model's discovery hop.
        Recovery may still justify a reread inside the hops that remain, but it
        can never mint a third ordinary request. After two executed rounds the
        next request is action-only even if both failed — ``report_blocker`` is
        the truthful answer when the evidence gathered is genuinely insufficient.
        """
        missing_one = tool_round([("f0", "read_file", {"path": "no_such_a.py"})])
        missing_two = tool_round([("f1", "read_file", {"path": "no_such_b.py"})])
        backend = ScriptedBackend([missing_one, missing_two, read_round("r2", 2)])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        failures = [r for r in recorder.of_type(ToolResult) if not r.ok]
        assert failures, "this test is only meaningful if the reads really failed"
        assert len(backend.ordinary_calls()) == 2, (
            "two failed-but-executed rounds spend both hops"
        )
        assert backend.calls[2].get("require_tool_call") is True, (
            "a failing turn must reach action-only, not keep circling"
        )


# ── 8: turns the stage does not govern ──────────────────────────────────────


class TestUngovernedTurns:

    def test_read_only_registry_is_untouched(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 8a: no mutation tools exist, so there is nothing to serialize."""
        backend = ScriptedBackend([
            read_round("r0", 0), read_round("r1", 1),
            read_round("r2", 2), read_round("r3", 3),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace, read_only=True), Recorder())

        assert not backend.focused_calls(), (
            "a read-only turn keeps its existing unbounded discovery"
        )

    def test_non_implementation_route_is_untouched(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 8b: only the implementation lane is staged."""
        backend = ScriptedBackend([
            read_round("r0", 0), read_round("r1", 1),
            read_round("r2", 2), read_round("r3", 3),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder(), route=RESEARCH_ROUTE)

        assert not backend.focused_calls()

    def test_absent_route_on_a_non_coding_request_is_untouched(
        self, workspace, isolated_streams
    ) -> None:
        """An omitted route is resolved, not ignored — and this one is chat.

        A caller that passes no route no longer switches the ceiling off; the
        send resolves one from the real user message. Here that message asks a
        question, so the resolved route is the chat lane and the turn keeps its
        existing unbounded discovery. The coding-request half of the same rule
        is ``test_resolved_route_reaches_focused_action`` in
        ``test_hybrid_route_discovery_ceiling.py``.
        """
        backend = ScriptedBackend([
            read_round("r0", 0), read_round("r1", 1),
            read_round("r2", 2), read_round("r3", 3),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(
            build_manager(workspace, "What does this project do?"),
            Recorder(),
            route=None,
        )

        assert not backend.focused_calls()


# ── 9: the focused action contract is unchanged ─────────────────────────────


class TestFocusedActionContractSurvives:

    def _reach_focused(self, workspace, isolated_streams) -> ScriptedBackend:
        backend = ScriptedBackend([
            read_round("r0", 0),
            read_round("r1", 1),
            tool_round([("w1", "write_file", {
                "path": "notes.md", "content": "# Notes\n\nacted\n",
            })]),
            final_round("Applied."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        run(build_manager(workspace), Recorder())
        return backend

    def test_focused_request_uses_thinking_off_only_for_itself(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 9a: the user's selection is untouched; this one request differs."""
        backend = self._reach_focused(workspace, isolated_streams)

        assert backend.calls[0]["thinking"] == SELECTED_THINKING
        assert backend.calls[1]["thinking"] == SELECTED_THINKING
        assert backend.calls[2]["thinking"] == FOCUSED_ACTION_THINKING

    def test_focused_request_exposes_mutations_plus_blocker_only(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 9b: the exact action catalog, no observation tools."""
        backend = self._reach_focused(workspace, isolated_streams)

        exposed = {
            str(t.get("function", {}).get("name", ""))
            for t in backend.calls[2].get("tools") or []
        }
        assert REPORT_BLOCKER in exposed
        assert "write_file" in exposed
        assert "read_file" not in exposed
        assert "grep_search" not in exposed

    def test_focused_request_requires_exactly_one_tool_call(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 9c: ``require_tool_call`` is set, and the one act really lands."""
        backend = self._reach_focused(workspace, isolated_streams)

        assert backend.calls[2].get("require_tool_call") is True
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )

    def test_ordinary_requests_never_set_require_tool_call(
        self, workspace, isolated_streams
    ) -> None:
        backend = self._reach_focused(workspace, isolated_streams)

        assert "require_tool_call" not in backend.calls[0]
        assert "require_tool_call" not in backend.calls[1]

    def test_blocker_is_available_when_evidence_ran_out(
        self, workspace, isolated_streams
    ) -> None:
        """The accepted tradeoff: a third hop becomes a visible blocker.

        A turn that genuinely needed one more sequential look does not circle —
        it reports, and the user can see and answer it.
        """
        backend = ScriptedBackend([
            read_round("r0", 0),
            read_round("r1", 1),
            tool_round([(
                "b1", REPORT_BLOCKER,
                {"blocker": "Need to inspect a third file before editing."},
            )]),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run(build_manager(workspace), recorder)

        assert backend.calls[2].get("require_tool_call") is True
        blocker_results = [
            r for r in recorder.of_type(ToolResult) if r.name == REPORT_BLOCKER
        ]
        assert blocker_results, "the blocker call is available and executes"


# ── 10: the instruction is ephemeral ────────────────────────────────────────


class TestInstructionIsEphemeral:

    def _run_to_final_request(self, workspace, isolated_streams):
        backend = ScriptedBackend([
            read_round("r0", 0), read_round("r1", 1), final_round("Done looking."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)
        return backend, recorder, manager

    def test_the_instruction_reaches_exactly_one_request(
        self, workspace, isolated_streams
    ) -> None:
        backend, _, _ = self._run_to_final_request(workspace, isolated_streams)

        carrying = [
            i for i, c in enumerate(backend.calls)
            if any(FINAL_EVIDENCE_INSTRUCTION in t for t in system_texts(c))
        ]
        assert carrying == [1], f"expected only request 1 to carry it, got {carrying}"

    def test_the_instruction_never_enters_persistent_history(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 10a: history is what persists, and it never sees the instruction."""
        _, _, manager = self._run_to_final_request(workspace, isolated_streams)

        rendered = json.dumps(manager.history.messages, default=str)
        assert FINAL_EVIDENCE_INSTRUCTION not in rendered

    def test_the_instruction_never_reaches_visible_chat(
        self, workspace, isolated_streams
    ) -> None:
        """Proof 10b: not streamed, not projected, not reasoning."""
        _, recorder, _ = self._run_to_final_request(workspace, isolated_streams)

        assert FINAL_EVIDENCE_INSTRUCTION not in recorder.chat_text
        assert FINAL_EVIDENCE_INSTRUCTION not in recorder.reasoning_text

    def test_the_instruction_opens_no_turn_boundary(
        self, workspace, isolated_streams
    ) -> None:
        """It is a system message: a user message would fake a new user turn."""
        backend, _, _ = self._run_to_final_request(workspace, isolated_streams)

        user_texts = [
            str(m.get("content", ""))
            for m in backend.calls[1].get("messages") or []
            if m.get("role") == "user"
        ]
        assert not any(FINAL_EVIDENCE_INSTRUCTION in t for t in user_texts)

    def test_a_later_request_is_built_from_clean_history(
        self, workspace, isolated_streams
    ) -> None:
        """The next request rebuilds from history that never saw it."""
        backend = ScriptedBackend([
            read_round("r0", 0), read_round("r1", 1), read_round("r2", 2),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

        run(build_manager(workspace), Recorder())

        assert not any(
            FINAL_EVIDENCE_INSTRUCTION in t for t in system_texts(backend.calls[2])
        ), "the focused request does not inherit the previous request's instruction"


# ── the stage as plain data ─────────────────────────────────────────────────


class TestStageUnit:
    """The stage is a small enum on per-turn state, not an engine."""

    def _state(
        self,
        *,
        lane: TaskLane = TaskLane.implementation,
        read_only: bool = False,
    ) -> _SendState:
        state = _SendState(
            mode="single",
            research_policy=None,
            task_route=TaskRoute(
                lane=lane, action="x", confidence=0.9, reason="unit"
            ),
            read_only=read_only,
        )
        return state

    def test_starts_at_survey(self) -> None:
        assert self._state().implementation_stage is ImplementationStage.SURVEY

    def test_two_executed_rounds_exhaust_it(self) -> None:
        state = self._state()
        assert state.consume_implementation_stage(executed=True)
        assert state.implementation_stage is ImplementationStage.FINAL_EVIDENCE
        assert state.consume_implementation_stage(executed=True)
        assert state.implementation_stage is ImplementationStage.EXHAUSTED
        assert state.implementation_stage_exhausted()

    def test_a_round_that_executed_nothing_consumes_nothing(self) -> None:
        state = self._state()
        for _ in range(10):
            state.consume_implementation_stage(executed=False)
        assert state.implementation_stage is ImplementationStage.SURVEY

    def test_exhaustion_is_not_reported_under_read_only(self) -> None:
        state = self._state(read_only=True)
        state.consume_implementation_stage(executed=True)
        state.consume_implementation_stage(executed=True)
        assert not state.implementation_stage_exhausted()

    def test_an_applied_write_ends_the_protocol(self) -> None:
        state = self._state()
        state.consume_implementation_stage(executed=True)
        state.consume_implementation_stage(executed=True)
        assert state.implementation_stage_exhausted()

        state.pre_edit_guard.observe_result(
            "write_file", True, json.dumps({"applied": True})
        )
        assert not state.implementation_stage_exhausted(), (
            "once a write applies the stage stops being consulted"
        )

    def test_probes_complete_nothing_until_a_write_lands(self) -> None:
        """A probe is not this turn's action while the edit has not happened."""
        state = self._state()
        assert not state.probes_complete_action()

        state.pre_edit_guard.observe_result(
            "write_file", True, json.dumps({"applied": True})
        )
        assert state.probes_complete_action(), (
            "after a real write, a validating command completes the action again"
        )

    def test_probes_still_complete_action_off_the_implementation_lane(self) -> None:
        """"Run the tests for me" is a turn whose whole point is the probe."""
        assert self._state(lane=TaskLane.research).probes_complete_action()
        assert self._state(lane=TaskLane.chat).probes_complete_action()
        assert self._state(read_only=True).probes_complete_action()

    def test_staging_predicate_is_pure_over_the_four_facts(self) -> None:
        guard = PreEditLoopGuard()
        base = {
            "mode": "single",
            "route": IMPLEMENTATION_ROUTE,
            "guard": guard,
            "read_only": False,
        }
        assert implementation_staging_applies(**base)
        assert not implementation_staging_applies(**{**base, "mode": "planner"})
        assert not implementation_staging_applies(**{**base, "read_only": True})
        assert not implementation_staging_applies(**{**base, "route": RESEARCH_ROUTE})
        assert not implementation_staging_applies(**{**base, "route": None})
        assert not implementation_staging_applies(**{**base, "guard": None})

        guard.observe_result("write_file", True, json.dumps({"applied": True}))
        assert not implementation_staging_applies(**base)

    def test_no_new_worker_or_phase_machine_was_added(self) -> None:
        """The stage is an enum plus three predicates — nothing more."""
        import aura.conversation.manager_send_state as module

        source = module.__doc__ or ""
        assert "ImplementationStage" in source
        members = {m.value for m in ImplementationStage}
        assert members == {"survey", "final_evidence", "exhausted"}, (
            "exactly three stages; a fourth would be a phase machine"
        )
