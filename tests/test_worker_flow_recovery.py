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


def test_zero_work_finalization_always_nudges(tmp_path) -> None:
    """Zero-work recovery no longer stalls or finishes — always nudges."""
    manager = _manager(tmp_path)
    state = _worker_state()
    events = []

    # First call: nudge
    result = _handle_worker_zero_work_final(
        state, manager.history, events.append, manager._finish_worker_unrecoverable,
    )
    assert result == "nudged"
    assert not any(isinstance(event, Done) for event in events)
    recovery_messages = [
        message["content"]
        for message in manager.history.messages
        if message.get("role") == "user"
    ]
    assert len(recovery_messages) == 1
    assert WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT in recovery_messages[0]

    # Second call: same conditions, still nudge (not finished)
    events.clear()
    result = _handle_worker_zero_work_final(
        state, manager.history, events.append, manager._finish_worker_unrecoverable,
    )
    assert result == "nudged"
    assert not any(isinstance(event, Done) for event in events)


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
