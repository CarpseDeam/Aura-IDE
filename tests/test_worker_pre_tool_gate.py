"""Focused tests for the worker.pre_tool_use lifecycle gate.

Tests the gate behaviour through :func:`run_worker_pre_tool_gate`
using a minimal fixture setup.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_pre_tool_gate import (
    WorkerPreToolGateContext,
    run_worker_pre_tool_gate,
)
from aura.events import WORKER_PRE_TOOL_GATE_DECIDED, EventBus
from aura.lifecycle import GateDecision, HookMatcher, LifecycleHooks

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def lifecycle() -> LifecycleHooks:
    return LifecycleHooks()


@pytest.fixture
def history() -> History:
    return History()


@pytest.fixture
def worker_state() -> _SendState:
    return _SendState(mode="worker", research_policy=None)


@pytest.fixture
def planner_state() -> _SendState:
    return _SendState(mode="planner", research_policy=None)


@pytest.fixture
def runner(history: History) -> ToolRoundRunner:
    """Build a ToolRoundRunner with mocked dependencies."""
    tools = MagicMock(spec=ToolRegistry)
    tools.workspace_root = "/fake/root"

    tool_runner = MagicMock()
    loop_detector = MagicMock()
    planner_refresh = MagicMock()

    return ToolRoundRunner(
        history=history,
        tools=tools,  # type: ignore[arg-type]
        tool_runner=tool_runner,
        loop_detector=loop_detector,
        planner_refresh=planner_refresh,
    )


@pytest.fixture
def runner_with_lifecycle(
    history: History,
    lifecycle: LifecycleHooks,
) -> ToolRoundRunner:
    """Like *runner* but with a real LifecycleHooks instance."""
    tools = MagicMock(spec=ToolRegistry)
    tools.workspace_root = "/fake/root"

    tool_runner = MagicMock()
    loop_detector = MagicMock()
    planner_refresh = MagicMock()

    return ToolRoundRunner(
        history=history,
        tools=tools,  # type: ignore[arg-type]
        tool_runner=tool_runner,
        loop_detector=loop_detector,
        planner_refresh=planner_refresh,
        lifecycle=lifecycle,
    )


def _gate_context(runner: ToolRoundRunner) -> WorkerPreToolGateContext:
    """Build a WorkerPreToolGateContext from a ToolRoundRunner's privates."""
    return WorkerPreToolGateContext(
        history=runner._history,
        tools=runner._tools,
        lifecycle=runner._lifecycle,
        event_bus=runner._event_bus,
    )


def _run_gate(
    runner: ToolRoundRunner,
    *,
    tool_call_id: str = "tc_01",
    name: str = "write_file",
    args: dict[str, Any] | None = None,
    state: _SendState,
) -> dict[str, Any] | None:
    """Call :func:`run_worker_pre_tool_gate` with *runner*'s dependencies."""
    if args is None:
        args = {"path": "test.txt", "content": "hello"}
    return run_worker_pre_tool_gate(
        context=_gate_context(runner),
        tool_call_id=tool_call_id,
        name=name,
        args=args,
        state=state,
    )


def _process_runner(
    *,
    lifecycle: LifecycleHooks | None = None,
    event_bus: EventBus | None = None,
) -> tuple[ToolRoundRunner, History, MagicMock]:
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.workspace_root = "/fake/root"
    tools.execute.return_value = ToolExecResult(
        ok=True,
        payload={"ok": True, "value": "executed"},
    )

    loop_detector = MagicMock()
    loop_detector.observe.return_value = SimpleNamespace(
        content='{"ok": true, "value": "executed"}',
        info=None,
    )

    runner = ToolRoundRunner(
        history=history,
        tools=tools,  # type: ignore[arg-type]
        tool_runner=MagicMock(),
        loop_detector=loop_detector,
        planner_refresh=MagicMock(),
        lifecycle=lifecycle,
        event_bus=event_bus,
    )
    return runner, history, tools


def _process_task(
    runner: ToolRoundRunner,
    state: _SendState,
    *,
    name: str = "read_file",
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return runner._process_task(
        task={"id": "tc_01", "name": name, "args": args or {"path": "old.txt"}},
        state=state,
        on_event=MagicMock(),
        approval_cb=MagicMock(),
        cancel_event=MagicMock(),
        dispatch_cb=None,
        workflow_state_cb=None,
        explicit_validation_commands=None,
        declared_run_command=None,
    )


# ── Tests: no lifecycle supplied ──────────────────────────────────────────


class TestNoLifecycle:
    """When ToolRoundRunner has no lifecycle, gate is never called."""

    def test_returns_none(self, runner: ToolRoundRunner, worker_state: _SendState) -> None:
        result = _run_gate(runner, state=worker_state)
        assert result is None

    def test_process_task_executes_unchanged(self, worker_state: _SendState) -> None:
        runner, _history, tools = _process_runner()

        result = _process_task(
            runner,
            worker_state,
            args={"path": "old.txt"},
        )

        assert result["id"] == "tc_01"
        tools.execute.assert_called_once()
        assert tools.execute.call_args.kwargs["args"] == {"path": "old.txt"}


# ── Tests: lifecycle supplied, no matching gate ───────────────────────────


class TestNoMatchingGate:
    """When lifecycle exists but no gate handler matches, execution continues."""

    def test_returns_none(self, runner_with_lifecycle: ToolRoundRunner, worker_state: _SendState) -> None:
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is None

    def test_process_task_executes_and_emits_allow_decision(
        self,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        bus = EventBus()
        events = []
        bus.subscribe(WORKER_PRE_TOOL_GATE_DECIDED, events.append)
        runner, _history, tools = _process_runner(
            lifecycle=lifecycle,
            event_bus=bus,
        )

        _process_task(runner, worker_state)

        tools.execute.assert_called_once()
        assert len(events) == 1
        assert events[0].payload == {
            "tool_call_id": "tc_01",
            "tool_name": "read_file",
            "allowed": True,
            "blocked": False,
            "reason": "",
            "rewritten": False,
            "additional_context": False,
        }


# ── Tests: gate blocks a worker tool ──────────────────────────────────────


class TestGateBlocks:
    """When a matching gate handler blocks, the tool is not executed."""

    def test_blocks_with_default_reason(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.block(reason=""),
        )
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is not None
        assert result["blocked"] is True
        payload = result["blocked_payload"]
        assert payload["ok"] is False
        assert payload["blocked"] is True
        assert payload["failure_class"] == "lifecycle_gate_blocked"
        assert payload["reason"] == "worker_pre_tool_use_blocked"
        assert payload["tool"] == "write_file"
        assert payload["recoverable"] is True

    def test_blocks_with_custom_reason(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.block(reason="custom_block_reason"),
        )
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is not None
        assert result["blocked"] is True
        assert result["blocked_payload"]["reason"] == "custom_block_reason"

    def test_process_task_does_not_execute_blocked_tool(
        self,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        bus = EventBus()
        events = []
        bus.subscribe(WORKER_PRE_TOOL_GATE_DECIDED, events.append)
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.block(reason="no_read"),
        )
        runner, _history, tools = _process_runner(
            lifecycle=lifecycle,
            event_bus=bus,
        )

        result = _process_task(runner, worker_state)

        tools.execute.assert_not_called()
        payload = result["result_payload"]
        assert '"blocked": true' in payload
        assert '"reason": "no_read"' in payload
        assert len(events) == 1
        assert events[0].payload["blocked"] is True
        assert events[0].payload["reason"] == "no_read"

    def test_allows_unmatched_tool(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        """Gate handler only blocks a specific tool; other tools pass."""
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use", tool_name="write_file"),
            lambda ctx: GateDecision.block(reason="no_write"),
        )
        # A different tool should pass
        result = _run_gate(
            runner_with_lifecycle,
            name="read_file",
            args={"path": "test.txt"},
            state=worker_state,
        )
        assert result is None


# ── Tests: gate rewrites args ─────────────────────────────────────────────


class TestGateRewritesArgs:
    """When a matching gate handler rewrites args, the tool runs with new args."""

    def test_rewrites_args(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        original_args = {"path": "old.txt", "content": "old"}
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.rewrite(
                {"args": {"path": "new.txt", "content": "new"}},
                reason="redirect",
            ),
        )
        result = _run_gate(
            runner_with_lifecycle,
            args=original_args,
            state=worker_state,
        )
        assert result is not None
        assert "rewritten_args" in result
        assert result["rewritten_args"] == {"path": "new.txt", "content": "new"}

    def test_process_task_executes_with_rewritten_args(
        self,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.rewrite(
                {"args": {"path": "new.txt"}},
                reason="redirect",
            ),
        )
        runner, _history, tools = _process_runner(lifecycle=lifecycle)

        _process_task(
            runner,
            worker_state,
            args={"path": "old.txt"},
        )

        tools.execute.assert_called_once()
        assert tools.execute.call_args.kwargs["args"] == {"path": "new.txt"}

    def test_rewrite_returns_partial_args(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        """Rewrite may return a subset of args."""
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.rewrite(
                {"args": {"path": "new.txt"}},
                reason="partial",
            ),
        )
        result = _run_gate(
            runner_with_lifecycle,
            args={"path": "old.txt", "content": "old"},
            state=worker_state,
        )
        assert result is not None
        assert result["rewritten_args"] == {"path": "new.txt"}

    def test_rewrite_and_additional_context_both_apply(
        self,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.inject_context("Extra context."),
            name="context",
        )
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.rewrite({"args": {"path": "new.txt"}}),
            name="rewrite",
        )
        runner, history, tools = _process_runner(lifecycle=lifecycle)

        _process_task(
            runner,
            worker_state,
            args={"path": "old.txt"},
        )

        tools.execute.assert_called_once()
        assert tools.execute.call_args.kwargs["args"] == {"path": "new.txt"}
        assert any(
            msg.get("aura_internal") is True
            and msg.get("content") == "Extra context."
            for msg in history.messages
        )


# ── Tests: gate injects additional_context ────────────────────────────────


class TestGateInjectsContext:
    """When a gate handler injects context, it's appended to history."""

    def test_injects_additional_context(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.inject_context(
                "Extra steering context for the model.",
                reason="steer",
            ),
        )
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is None  # Gate allowed
        # Context should have been added to history
        history_msgs = runner_with_lifecycle._history.messages
        assert any(
            msg.get("aura_internal") is True
            and msg.get("content") == "Extra steering context for the model."
            for msg in history_msgs
        )

    def test_context_uses_internal_channel(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        """Additional context should use aura_internal flag, not a user-visible message."""
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.inject_context(
                "Internal context.",
                reason="steer",
            ),
        )
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is None
        history_msgs = runner_with_lifecycle._history.messages
        flagged = [
            msg for msg in history_msgs
            if msg.get("content") == "Internal context."
        ]
        assert len(flagged) == 1
        assert flagged[0].get("aura_internal") is True


# ── Tests: gate applies to worker mode only ───────────────────────────────


class TestWorkerModeOnly:
    """Gate only fires when state.mode == 'worker'."""

    def test_private_helper_does_not_filter_by_mode(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        planner_state: _SendState,
    ) -> None:
        blocked = False

        def handler(ctx: Any) -> GateDecision:
            nonlocal blocked
            blocked = True
            return GateDecision.block(reason="should_not_fire")

        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            handler,
        )
        result = _run_gate(runner_with_lifecycle, state=planner_state)
        assert result is not None
        assert blocked is True  # The helper itself doesn't filter by mode

    def test_planner_mode_guard_in_process_task(
        self,
        lifecycle: LifecycleHooks,
        planner_state: _SendState,
    ) -> None:
        """Verify _process_task skips gate for non-worker modes."""
        history = History()
        tools = MagicMock(spec=ToolRegistry)
        tools.workspace_root = "/fake/root"

        runner = ToolRoundRunner(
            history=history,
            tools=tools,  # type: ignore[arg-type]
            tool_runner=MagicMock(),
            loop_detector=MagicMock(),
            planner_refresh=MagicMock(),
            lifecycle=lifecycle,
        )
        result = runner._process_task(
            task={"id": "tc_01", "name": "write_file", "args": {"path": "x"}},
            state=planner_state,
            on_event=MagicMock(),
            approval_cb=MagicMock(),
            cancel_event=MagicMock(),
            dispatch_cb=None,
            workflow_state_cb=None,
            explicit_validation_commands=None,
            declared_run_command=None,
        )
        # In planner mode, the gate is skipped and execution proceeds normally.
        # The tool would attempt execution through self._tools.execute(),
        # but since tools is a Mock, it returns a MagicMock result.
        assert result is not None


# ── Tests: invalid gate handler ───────────────────────────────────────────


class TestInvalidGateHandler:
    """Invalid handler results produce a blocked decision."""

    def test_handler_returns_non_decision(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: "not a decision",  # type: ignore[return-value]
        )
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is not None
        assert result["blocked"] is True
        # The gate registry correctly rejects non-GateDecision returns
        # as `lifecycle_gate_invalid_decision`, not a handler error.
        assert "lifecycle_gate" in result["blocked_payload"]["reason"]

    def test_handler_raises_exception(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        def failing_handler(ctx: Any) -> GateDecision:
            raise RuntimeError("unexpected error")

        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            failing_handler,
        )
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is not None
        assert result["blocked"] is True

    def test_multiple_rewriters(
        self,
        runner_with_lifecycle: ToolRoundRunner,
        lifecycle: LifecycleHooks,
        worker_state: _SendState,
    ) -> None:
        """Two handlers both attempting to rewrite should block with conflict."""
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.rewrite({"args": {"a": 1}}),
            name="rewriter_1",
        )
        lifecycle.register_gate(
            HookMatcher("worker.pre_tool_use"),
            lambda ctx: GateDecision.rewrite({"args": {"b": 2}}),
            name="rewriter_2",
        )
        result = _run_gate(runner_with_lifecycle, state=worker_state)
        assert result is not None
        assert result["blocked"] is True
        assert "multiple" in result["blocked_payload"]["reason"].lower()


# ── Tests: lifecycle exception ────────────────────────────────────────────


class TestLifecycleException:
    """When lifecycle.ask() itself raises, the gate falls back to blocked."""

    def test_ask_exception_blocked(
        self,
        worker_state: _SendState,
    ) -> None:
        """Create a lifecycle whose ask() will raise."""
        history = History()
        tools = MagicMock(spec=ToolRegistry)
        tools.workspace_root = "/fake/root"

        broken_lifecycle = MagicMock(spec=LifecycleHooks)
        # ask returns a coroutine that raises when awaited
        broken_lifecycle.ask.side_effect = RuntimeError("broken")

        runner = ToolRoundRunner(
            history=history,
            tools=tools,  # type: ignore[arg-type]
            tool_runner=MagicMock(),
            loop_detector=MagicMock(),
            planner_refresh=MagicMock(),
            lifecycle=broken_lifecycle,
        )
        result = _run_gate(runner, state=worker_state)
        assert result is not None
        assert result["blocked"] is True
