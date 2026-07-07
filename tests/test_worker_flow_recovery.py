from __future__ import annotations

from aura.client import Done
from aura.conversation.history import History
from aura.conversation.manager import (
    ConversationManager,
    WORKER_FLOW_THRASH_RECOVERY_BUDGET,
    WORKER_FLOW_ZERO_WORK_RECOVERY_BUDGET,
)
from aura.conversation.manager_send_state import _SendState
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_flow import WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT


def _manager(tmp_path) -> ConversationManager:
    return ConversationManager(
        History(),
        ToolRegistry(workspace_root=tmp_path, mode="worker"),
    )


def _worker_state() -> _SendState:
    return _SendState(mode="worker", research_policy=None)


def test_zero_work_finalization_spends_internal_recovery_budget(tmp_path) -> None:
    manager = _manager(tmp_path)
    state = _worker_state()
    events = []

    for _ in range(WORKER_FLOW_ZERO_WORK_RECOVERY_BUDGET):
        assert manager._handle_worker_zero_work_final(state, events.append) == "nudged"

    assert state.worker_flow_zero_work_recovery_count == WORKER_FLOW_ZERO_WORK_RECOVERY_BUDGET
    assert not any(isinstance(event, Done) for event in events)
    recovery_messages = [
        message["content"]
        for message in manager.history.messages
        if message.get("role") == "user"
    ]
    assert len(recovery_messages) == WORKER_FLOW_ZERO_WORK_RECOVERY_BUDGET
    assert WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT in recovery_messages[0]
    assert "repeated internal continuation" in recovery_messages[-1]

    assert manager._handle_worker_zero_work_final(state, events.append) == "finished"
    assert any(isinstance(event, Done) for event in events)


def test_repeated_worker_flow_orientation_spends_thrash_budget(tmp_path) -> None:
    manager = _manager(tmp_path)
    state = _worker_state()
    state.worker_flow_nudge_count = 1
    state.write_attempts_by_path["aura/example.py"] = 1
    events = []

    for _ in range(WORKER_FLOW_THRASH_RECOVERY_BUDGET):
        assert state.worker_flow is not None
        state.worker_flow.state.pending_steering_message = "Worker Flow: steer."
        state.worker_flow.state.pending_steering_reason = "orientation"
        assert manager._handle_worker_flow_steering(state, events.append) == "nudged"

    assert state.worker_flow_thrash_recovery_count == WORKER_FLOW_THRASH_RECOVERY_BUDGET
    assert not any(isinstance(event, Done) for event in events)

    assert state.worker_flow is not None
    state.worker_flow.state.pending_steering_message = "Worker Flow: steer."
    state.worker_flow.state.pending_steering_reason = "orientation"
    assert manager._handle_worker_flow_steering(state, events.append) == "finished"
    assert any(isinstance(event, Done) for event in events)


def test_artifact_item_zero_fresh_writes_validation_pass_finished_immediately(tmp_path) -> None:
    """Artifact item with zero writes and validation passed finishes without recovery nudge."""
    manager = _manager(tmp_path)
    state = _worker_state()
    state.worker_artifact_id = "artifact-1"
    state.worker_artifact_item_id = "item-1"
    state.worker_explicit_validation_passed = True
    # No writes, no write attempts, no candidate final message (clean state).
    events: list = []

    result = manager._handle_worker_zero_work_final(state, events.append)

    assert result == "finished"
    # No recovery prompt should have been appended to history.
    recovery_messages = [
        message["content"]
        for message in manager.history.messages
        if message.get("role") == "user"
    ]
    assert len(recovery_messages) == 0
    # A Done event signals validated completion.
    assert any(isinstance(event, Done) for event in events)
