"""A probe is not a completed action while the turn has not acted.

``task_completion_context`` means "this turn did its work and now owes a final
response".  It is set from :data:`ACTION_COMPLETION_TOOL_NAMES`, which mixes two
unlike things: writes (the action itself) and probes — ``git_status``,
``git_diff``, ``run_diagnostic_command``, the terminal tools — which only look.

Counting a successful probe as the completed action is right on a turn whose
whole point is the probe ("what's the git status?", "run the tests").  On an
implementation turn that has not yet written anything it is false, and it was
expensive: ``task_completion_context`` vetoes the focused action transition, so
one successful ``git_status`` early in an implementation turn convinced the loop
the turn had already acted and permanently suppressed the transition that would
have ended it.  The model could then read new files indefinitely.

The fix is at the source — the flag is simply not set by a probe before the
first applied write — so it holds regardless of any discovery-stage policy
layered above it.
"""

from __future__ import annotations

import json

import pytest

import aura.conversation.manager as manager_module
import tests.test_implementation_discovery_stage as stage_tests
from aura.conversation.completion_guard import (
    ACTION_COMPLETION_TOOL_NAMES,
    TASK_COMPLETION_TOOL_NAMES,
    terminal_result_completed,
    tool_result_completes_action,
)
from aura.conversation.focused_action import (
    FocusedActionState,
    should_enter_focused_action,
)
from aura.conversation.manager_send_state import _SendState
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tool_limits import WRITE_TOOLS
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry

PROBES = sorted(TASK_COMPLETION_TOOL_NAMES)
WRITES = sorted(WRITE_TOOLS)


def implementation_state(
    *, lane: TaskLane = TaskLane.implementation, read_only: bool = False
) -> _SendState:
    return _SendState(
        mode="single",
        research_policy=None,
        task_route=TaskRoute(
            lane=lane, action="x", confidence=0.9, reason="unit"
        ),
        read_only=read_only,
    )


class TestProbesVersusWrites:

    @pytest.mark.parametrize("name", PROBES)
    def test_a_probe_completes_nothing_when_the_turn_has_not_acted(
        self, name: str
    ) -> None:
        assert not tool_result_completes_action(
            name, True, probes_complete_action=False
        )

    @pytest.mark.parametrize("name", PROBES)
    def test_a_probe_completes_the_action_by_default(self, name: str) -> None:
        """Every other caller keeps the historical behaviour untouched."""
        assert tool_result_completes_action(name, True)

    @pytest.mark.parametrize("name", WRITES)
    def test_a_write_always_completes_the_action(self, name: str) -> None:
        """The write *is* the action; the probe rule must never gate it."""
        assert tool_result_completes_action(
            name, True, probes_complete_action=False
        )

    @pytest.mark.parametrize("name", PROBES + WRITES)
    def test_a_failed_call_completes_nothing(self, name: str) -> None:
        assert not tool_result_completes_action(name, False)
        assert not tool_result_completes_action(
            name, False, probes_complete_action=False
        )

    def test_an_unrelated_tool_completes_nothing(self) -> None:
        assert not tool_result_completes_action("read_file", True)
        assert not tool_result_completes_action("grep_search", True)

    def test_the_tool_sets_are_still_the_documented_union(self) -> None:
        assert ACTION_COMPLETION_TOOL_NAMES == (
            TASK_COMPLETION_TOOL_NAMES | WRITE_TOOLS
        )


class TestTerminalCompletion:
    """A terminal round is wholly a probe, so the gate applies to all of it."""

    CLEAN = {"_terminal_payload": {"exit_code": 0}}

    def test_a_clean_exit_completes_the_action_by_default(self) -> None:
        assert terminal_result_completed(self.CLEAN)

    def test_a_clean_exit_completes_nothing_when_the_turn_has_not_acted(
        self,
    ) -> None:
        assert not terminal_result_completed(
            self.CLEAN, probes_complete_action=False
        )

    @pytest.mark.parametrize(
        "info",
        [None, {}, {"_terminal_payload": {"exit_code": 1}}, {"_terminal_payload": 3}],
    )
    def test_anything_but_a_clean_exit_completes_nothing(self, info) -> None:
        assert not terminal_result_completed(info)


class TestStateOwnsTheAnswer:
    """``_SendState`` decides; the guard only applies what it is told."""

    def test_an_implementation_turn_pre_write_says_no(self) -> None:
        assert not implementation_state().probes_complete_action()

    def test_an_applied_write_restores_probe_completion(self) -> None:
        """After a real edit, a validating command completes the action again."""
        state = implementation_state()
        state.pre_edit_guard.observe_result(
            "write_file", True, json.dumps({"applied": True})
        )
        assert state.probes_complete_action()

    def test_a_write_that_did_not_apply_does_not_restore_it(self) -> None:
        """Fail-closed, like every other applied-write check in the loop."""
        state = implementation_state()
        state.pre_edit_guard.observe_result(
            "write_file", True, json.dumps({"applied": False})
        )
        assert not state.probes_complete_action()

    @pytest.mark.parametrize(
        "lane",
        [TaskLane.research, TaskLane.chat, TaskLane.validation, TaskLane.built_in_action],
    )
    def test_other_lanes_are_untouched(self, lane: TaskLane) -> None:
        """"Run the tests for me" is a turn whose point is the probe."""
        assert implementation_state(lane=lane).probes_complete_action()

    def test_read_only_is_untouched(self) -> None:
        """No mutation tools exist, so no write is coming to wait for."""
        assert implementation_state(read_only=True).probes_complete_action()

    def test_worker_and_planner_modes_are_untouched(self) -> None:
        for mode in ("worker", "planner"):
            state = _SendState(mode=mode, research_policy=None)
            assert state.probes_complete_action()


class TestTheResidualOverride:
    """What the ``stage_exhausted`` override in ``focused_action`` still covers.

    With probes fixed at the source, that override is no longer what saves the
    probe case — either fix closes it alone. It is not dead code, though: a
    *write* that returns ``ok`` while its payload never says ``applied: True``
    still sets ``task_completion_context`` without ever setting
    ``guard.write_applied``. The turn believes it acted, nothing landed, and the
    veto would otherwise suppress the transition permanently.
    """

    def _args(self, **over):
        guard = PreEditLoopGuard()
        base = dict(
            mode="single",
            route=TaskRoute(
                lane=TaskLane.implementation,
                action="implementation",
                confidence=0.9,
                reason="unit",
            ),
            guard=guard,
            task_completion_context=False,
            state=FocusedActionState(),
            stage_exhausted=False,
        )
        base.update(over)
        return base

    def test_a_completion_context_alone_still_vetoes(self) -> None:
        args = self._args(task_completion_context=True)
        args["guard"].focused = True
        assert not should_enter_focused_action(**args)

    def test_an_exhausted_stage_overrides_an_unearned_completion(self) -> None:
        assert should_enter_focused_action(
            **self._args(task_completion_context=True, stage_exhausted=True)
        )

    def test_it_can_never_override_a_completion_a_real_write_earned(self) -> None:
        """``stage_exhausted`` is unreachable once a write applied — by construction."""
        args = self._args(task_completion_context=True, stage_exhausted=True)
        args["guard"].observe_result(
            "write_file", True, json.dumps({"applied": True})
        )
        assert not should_enter_focused_action(**args), (
            "an applied write leaves the pre-write protocol entirely"
        )


class TestThroughTheRealLoop:
    """The flag really stays clear, and the veto it fed really lifts."""

    def _spy_on_send_state(self, monkeypatch) -> list:
        """Capture the live ``_SendState`` the manager builds for the turn."""
        import aura.conversation.manager_tool_round as mtr

        seen: list = []
        original = mtr.ToolRoundRunner.run

        def spy(self, *args, **kwargs):
            state = kwargs.get("state")
            if state is not None and state not in seen:
                seen.append(state)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(mtr.ToolRoundRunner, "run", spy)
        return seen

    def test_a_probe_never_sets_completion_context_pre_write(
        self, monkeypatch, tmp_path
    ) -> None:
        registry = ModelStreamRegistry()
        monkeypatch.setattr(manager_module, "model_streams", registry)
        seen = self._spy_on_send_state(monkeypatch)

        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.py").write_text("# a\n", encoding="utf-8")

        backend = stage_tests.ScriptedBackend([
            stage_tests.tool_round([
                ("p0", "run_diagnostic_command", {"command": "python --version"}),
            ]),
            stage_tests.final_round("Looked around."),
        ])
        registry.register(PRODUCTION_STREAM_HOOK, backend.stream)

        stage_tests.run(
            stage_tests.build_manager(root), stage_tests.Recorder()
        )

        assert seen, "the spy never saw a tool round"
        assert not seen[0].task_completion_context, (
            "a successful probe before any write completed nothing"
        )

    def test_the_same_probe_does_set_it_off_the_implementation_lane(
        self, monkeypatch, tmp_path
    ) -> None:
        """The historical behaviour, preserved where it was always correct."""
        registry = ModelStreamRegistry()
        monkeypatch.setattr(manager_module, "model_streams", registry)
        seen = self._spy_on_send_state(monkeypatch)

        root = tmp_path / "proj"
        root.mkdir()

        backend = stage_tests.ScriptedBackend([
            stage_tests.tool_round([
                ("p0", "run_diagnostic_command", {"command": "python --version"}),
            ]),
            stage_tests.final_round("Here is the status."),
        ])
        registry.register(PRODUCTION_STREAM_HOOK, backend.stream)

        stage_tests.run(
            stage_tests.build_manager(root, "Run the version check for me."),
            stage_tests.Recorder(),
            route=stage_tests.RESEARCH_ROUTE,
        )

        assert seen[0].task_completion_context, (
            "a turn whose point is the probe still completes on the probe"
        )
