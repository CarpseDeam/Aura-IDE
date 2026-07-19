"""Focused regressions for pre-Worker Planner dispatch recovery."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from aura.bridge.dispatch import _recoverable_plan_creation_failure
from aura.client import Done
from aura.conversation.dispatch import WorkerDispatchResult
from aura.conversation.dispatch_failure import (
    MAX_PLANNER_DISPATCH_RECOVERY_FAILURES,
    PLANNER_DISPATCH_RECOVERY_EXHAUSTED_REASON,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.planner_dispatch_gate import maybe_force_worker_dispatch
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.workflow_state import WorkflowState, WorkflowStatus
from aura.gui.cards.plan_writer_card import PlanWriterCard
from aura.gui.chat_view import ChatView
from aura.model_streams import model_streams


def _valid_dispatch() -> dict[str, Any]:
    return {
        "goal": "Fix Planner dispatch recovery.",
        "files": ["aura/conversation/manager.py"],
        "spec": "Correct the Planner dispatch recovery lifecycle without changing Worker execution.",
        "acceptance": "Run focused pytest regressions and confirm exactly one Worker starts.",
        "summary": "Repair Planner dispatch recovery.",
    }


def _two_item_dispatch() -> dict[str, Any]:
    payload = _valid_dispatch()
    payload["work_artifact"] = {
        "goal": payload["goal"],
        "items": [
            {
                "id": "control-flow",
                "title": "Repair control flow",
                "intent": "Keep failed dispatches in the Planner turn.",
                "target_files": ["aura/conversation/manager.py"],
                "acceptance": "The corrective Planner round runs.",
            },
            {
                "id": "ui-state",
                "title": "Project repair state",
                "intent": "Show repair without a terminal failure card.",
                "target_files": ["aura/gui/cards/plan_writer_card.py"],
                "acceptance": "The plan card becomes ready after repair.",
            },
        ],
    }
    return payload


def _tool_call(call_id: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "dispatch_to_worker",
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def _tool_round(call_id: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": "",
        "tool_calls": [_tool_call(call_id, arguments)],
    }


def _run_manager(
    tmp_path: Path,
    rounds: list[dict[str, Any]],
    dispatch_cb,
    *,
    user_text: str = "Fix Planner dispatch recovery in two steps.",
) -> tuple[History, list[list[dict[str, Any]]], list[Any]]:
    history = History(messages=[{"role": "user", "content": user_text}])
    manager = ConversationManager(history, ToolRegistry(tmp_path, mode="planner"))
    model_inputs: list[list[dict[str, Any]]] = []
    events: list[Any] = []

    def stream(**kwargs):
        model_inputs.append(kwargs["messages"])
        message = rounds[len(model_inputs) - 1]
        yield Done(
            finish_reason="tool_calls" if message.get("tool_calls") else "stop",
            full_message=message,
        )

    hook = f"_test_planner_dispatch_recovery_{id(rounds)}"
    model_streams.register(hook, stream)
    try:
        manager.send(
            on_event=events.append,
            approval_cb=lambda _request: ApprovalDecision(action="approve"),
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
            dispatch_cb=dispatch_cb,
            hook_name=hook,
            max_tool_rounds=10,
        )
    finally:
        model_streams.unregister(hook)
    return history, model_inputs, events


def test_malformed_dispatch_arguments_receive_corrective_planner_round(tmp_path: Path) -> None:
    dispatches: list[str] = []
    history, model_inputs, _events = _run_manager(
        tmp_path,
        [
            _tool_round("bad-json", '{"goal": "broken"'),
            _tool_round("fixed", _valid_dispatch()),
        ],
        lambda tool_id, _request: (
            dispatches.append(tool_id),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        user_text="Fix aura/conversation/manager.py.",
    )

    assert len(model_inputs) == 2
    assert dispatches == ["fixed"]
    assert "malformed_dispatch_arguments" in model_inputs[1][-1]["content"]
    tool_payload = json.loads(next(m["content"] for m in history.messages if m.get("role") == "tool"))
    assert tool_payload["extras"]["failure_class"] == "malformed_dispatch_arguments"


def test_scope_rejection_receives_corrected_dispatch(tmp_path: Path) -> None:
    dispatches: list[str] = []
    one_item = _valid_dispatch()
    _history, model_inputs, _events = _run_manager(
        tmp_path,
        [_tool_round("too-small", one_item), _tool_round("corrected", _two_item_dispatch())],
        lambda tool_id, _request: (
            dispatches.append(tool_id),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        user_text="1. Fix Planner recovery.\n2. Update the plan card.",
    )

    assert dispatches == ["corrected"]
    assert "planner_dispatch_scope_incomplete" in model_inputs[1][-1]["content"]


def test_plan_serialization_failure_receives_corrective_round(tmp_path: Path) -> None:
    callback_attempts: list[str] = []

    def dispatch(tool_id, _request):
        callback_attempts.append(tool_id)
        if len(callback_attempts) == 1:
            return _recoverable_plan_creation_failure(
                ValueError("Every work item must have a title.")
            )
        return WorkerDispatchResult(ok=True, summary="Worker completed.")

    _history, model_inputs, _events = _run_manager(
        tmp_path,
        [_tool_round("bad-plan", _valid_dispatch()), _tool_round("repaired-plan", _valid_dispatch())],
        dispatch,
        user_text="Fix aura/conversation/manager.py.",
    )

    assert callback_attempts == ["bad-plan", "repaired-plan"]
    assert "dispatch_serialization_failed" in model_inputs[1][-1]["content"]
    assert "Every work item must have a title." in model_inputs[1][-1]["content"]


def test_failed_attempt_then_valid_dispatch_starts_exactly_one_worker(tmp_path: Path) -> None:
    dispatches: list[str] = []
    _run_manager(
        tmp_path,
        [_tool_round("missing-fields", {}), _tool_round("valid", _valid_dispatch())],
        lambda tool_id, _request: (
            dispatches.append(tool_id),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        user_text="Fix aura/conversation/manager.py.",
    )
    assert dispatches == ["valid"]


def test_failed_attempts_do_not_satisfy_completion_gate() -> None:
    decision = maybe_force_worker_dispatch(
        latest_user_text="Fix aura/conversation/manager.py.",
        candidate_message={"role": "assistant", "content": "I will fix the manager."},
        planner_tool_calls_seen=1,
        dispatch_calls_seen=1,
        dispatch_accepted=False,
        dispatch_recovery_required=True,
        dispatch_recovery_message="exact corrective failure",
        already_steered=True,
    )
    assert decision.should_continue is True
    assert decision.steering_message == "exact corrective failure"


def test_empty_planner_output_after_failed_attempt_cannot_end_turn(tmp_path: Path) -> None:
    dispatches: list[str] = []
    _history, model_inputs, _events = _run_manager(
        tmp_path,
        [
            _tool_round("missing-fields", {}),
            {"role": "assistant", "content": "", "reasoning_content": ""},
            _tool_round("valid", _valid_dispatch()),
        ],
        lambda tool_id, _request: (
            dispatches.append(tool_id),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        user_text="Fix aura/conversation/manager.py.",
    )
    assert len(model_inputs) == 3
    assert dispatches == ["valid"]


def test_plan_card_transitions_writing_repairing_ready() -> None:
    app = QApplication.instance() or QApplication([])
    chat = ChatView()
    chat.add_tool_call("failed-plan", "dispatch_to_worker")
    card = chat.get_plan_writer_card("failed-plan")
    assert card is not None
    card.resize(600, 30)
    card._status.resize(560, 24)
    assert card._state == PlanWriterCard.STATE_RUNNING

    recoverable = WorkerDispatchResult(
        ok=False,
        summary="Plan scope was rejected.",
        recoverable=True,
        extras={"dispatch_not_started": True, "recoverable": True},
    )
    chat.set_tool_result(
        "failed-plan",
        True,
        json.dumps(recoverable.to_tool_payload()),
    )
    assert card._state == PlanWriterCard.STATE_REPAIRING
    assert "Repairing plan" in card._status.text()

    chat.add_tool_call("corrected-plan", "dispatch_to_worker")
    assert chat.get_plan_writer_card("failed-plan") is None
    assert chat.get_plan_writer_card("corrected-plan") is card

    ready = WorkflowState.intent_captured("corrected-plan", "Fix recovery").with_status(
        WorkflowStatus.plan_ready
    )
    card.update_workflow_state(ready)
    assert card._state == PlanWriterCard.STATE_DONE
    assert "Plan ready" in card._status.text()
    chat.deleteLater()
    app.processEvents()


def test_recovery_exhaustion_ends_with_exact_final_reason(tmp_path: Path) -> None:
    malformed = [_tool_round(f"bad-{i}", "{") for i in range(MAX_PLANNER_DISPATCH_RECOVERY_FAILURES)]
    history, model_inputs, events = _run_manager(
        tmp_path,
        malformed,
        lambda _tool_id, _request: (_ for _ in ()).throw(
            AssertionError("Worker must not start")
        ),
        user_text="Fix aura/conversation/manager.py.",
    )
    tool_payloads = [
        json.loads(message["content"])
        for message in history.messages
        if message.get("role") == "tool"
    ]
    exact_final_failure = tool_payloads[-1]["extras"]["quality_errors"][0]
    expected = PLANNER_DISPATCH_RECOVERY_EXHAUSTED_REASON.format(
        attempts=MAX_PLANNER_DISPATCH_RECOVERY_FAILURES,
        failure=exact_final_failure,
    )
    assert len(model_inputs) == MAX_PLANNER_DISPATCH_RECOVERY_FAILURES
    assert history.messages[-1]["content"] == expected
    assert [event for event in events if isinstance(event, Done)][-1].full_message["content"] == expected


def test_cancellation_remains_terminal(tmp_path: Path) -> None:
    calls: list[str] = []
    _history, model_inputs, _events = _run_manager(
        tmp_path,
        [_tool_round("cancelled", _valid_dispatch())],
        lambda tool_id, _request: (
            calls.append(tool_id),
            WorkerDispatchResult(
                ok=False,
                summary="Cancelled",
                cancelled=True,
                recoverable=False,
                extras={"dispatch_not_started": True, "dispatch_cancelled": True},
            ),
        )[1],
        user_text="Fix aura/conversation/manager.py.",
    )
    assert calls == ["cancelled"]
    assert len(model_inputs) == 1


def test_successful_first_attempt_dispatch_is_unchanged(tmp_path: Path) -> None:
    calls: list[str] = []
    history, model_inputs, _events = _run_manager(
        tmp_path,
        [_tool_round("first", _valid_dispatch())],
        lambda tool_id, _request: (
            calls.append(tool_id),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        user_text="Fix aura/conversation/manager.py.",
    )
    assert calls == ["first"]
    assert len(model_inputs) == 1
    payload = json.loads(next(m["content"] for m in history.messages if m.get("role") == "tool"))
    assert payload["ok"] is True
    assert payload["summary"] == "Worker completed."
