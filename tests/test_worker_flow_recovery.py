from __future__ import annotations

from aura.client import Done
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.manager_send_state import _SendState
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_finalization_gate import (
    _handle_worker_zero_work_final,
    handle_worker_candidate_finalization,
)
from aura.conversation.worker_flow import WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT


def _manager(tmp_path) -> ConversationManager:
    return ConversationManager(
        History(),
        ToolRegistry(workspace_root=tmp_path, mode="worker"),
    )


def _worker_state() -> _SendState:
    return _SendState(mode="worker", research_policy=None)


def test_zero_work_finalization_uses_progress_monitor(tmp_path) -> None:
    """Zero-work recovery uses the progress monitor: first call nudges, repeat with
    identical fingerprint and no writes stalls and finishes."""
    manager = _manager(tmp_path)
    state = _worker_state()
    events = []

    # First call: no prior state → progressing → nudge
    result = _handle_worker_zero_work_final(
        state, manager.history, events.append, manager._finish_worker_unrecoverable,
    )
    assert result == "nudged"

    assert state.progress_monitor is not None
    assert state.progress_monitor._last_fingerprint == "zero_work|zero_work_final|"
    assert not any(isinstance(event, Done) for event in events)
    recovery_messages = [
        message["content"]
        for message in manager.history.messages
        if message.get("role") == "user"
    ]
    assert len(recovery_messages) == 1
    assert WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT in recovery_messages[0]

    # Second call: same fingerprint, no writes → stalled → finished
    result = _handle_worker_zero_work_final(
        state, manager.history, events.append, manager._finish_worker_unrecoverable,
    )
    assert result == "finished"
    assert any(isinstance(event, Done) for event in events)


def test_repeated_worker_flow_orientation_uses_progress_monitor(tmp_path) -> None:
    """Thrash recovery uses the progress monitor: first call nudges, repeat with
    identical fingerprint and no writes stalls and finishes."""
    manager = _manager(tmp_path)
    state = _worker_state()
    state.worker_flow_nudge_count = 1
    state.write_attempts_by_path["aura/example.py"] = 1
    events = []

    # First call: no prior thrash state → progressing → nudge
    assert state.worker_flow is not None
    state.worker_flow.state.pending_steering_message = "Worker Flow: steer."
    state.worker_flow.state.pending_steering_reason = "orientation"
    assert manager._handle_worker_flow_steering(state, events.append) == "nudged"
    assert not any(isinstance(event, Done) for event in events)

    # Second call: same reason+steering, no writes → stalled → finished
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
    state.mark_explicit_validation_passed()
    events: list = []

    full_message = {"role": "assistant", "content": "{}"}
    result = handle_worker_candidate_finalization(
        state=state,
        full_message=full_message,
        history=manager.history,
        workspace_root=str(tmp_path),
        on_event=events.append,
        finish_worker_recoverable_followup=manager._finish_worker_unrecoverable,
        handle_worker_flow_steering=manager._handle_worker_flow_steering,
        declared_run_command=None,
        explicit_validation_commands=None,
    )

    assert result == "finished"
    # No recovery prompt should have been appended to history.
    recovery_messages = [
        message["content"]
        for message in manager.history.messages
        if message.get("role") == "user"
    ]
    assert len(recovery_messages) == 0


def test_artifact_item_steering_validation_pass_finished_immediately(tmp_path) -> None:
    """Artifact item with nudge count>0 and validation passed finishes in steering without thrash."""
    manager = _manager(tmp_path)
    state = _worker_state()
    state.worker_artifact_id = "artifact-1"
    state.worker_artifact_item_id = "item-1"
    state.mark_explicit_validation_passed()
    state.worker_flow_nudge_count = 1
    # No writes, no write attempts (zero-work branch would fire — artifact bailout must beat it).
    state.worker_flow.state.pending_steering_message = "Worker Flow: steer."
    state.worker_flow.state.pending_steering_reason = "orientation"
    events: list = []

    result = manager._handle_worker_flow_steering(state, events.append)

    assert result == "finished"
    # No recovery or thrash prompt should have been appended to history.
    recovery_messages = [
        message["content"]
        for message in manager.history.messages
        if message.get("role") == "user"
    ]
    assert len(recovery_messages) == 0
    # A Done event signals validated completion.
    assert any(isinstance(event, Done) for event in events)
