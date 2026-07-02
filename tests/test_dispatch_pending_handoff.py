from types import SimpleNamespace

from aura.bridge.dispatch_pending import DispatchPendingMap
from aura.bridge.dispatch_session import DispatchSession
from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch_plan import WorkerDispatchPlan, WorkerStepSpec
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.events import EventBus


def _request() -> WorkerDispatchRequest:
    return WorkerDispatchRequest(
        goal="Fix the bridge handoff.",
        files=["aura/bridge/dispatch.py"],
        spec="Ensure dispatch decisions wake the pending bridge handoff.",
        acceptance="Worker starts or a visible harness error is returned.",
        summary="Fix bridge handoff.",
    )


def test_pending_resolve_dispatched_sets_decision_and_edited_request():
    pending_map = DispatchPendingMap()
    pending = pending_map.register("call_dispatch", _request())

    resolved = pending_map.resolve_dispatched(
        "call_dispatch",
        goal="Edited goal",
        files=["aura/bridge/dispatch.py", "aura/bridge/dispatch_pending.py"],
        spec="Edited spec",
        acceptance="Edited acceptance",
        summary="Edited summary",
    )

    assert resolved is True
    assert pending.decision_event.is_set()
    assert pending.edited_request is not None
    assert pending.edited_request.goal == "Edited goal"
    assert pending.edited_request.files == [
        "aura/bridge/dispatch.py",
        "aura/bridge/dispatch_pending.py",
    ]
    assert pending.edited_request.spec == "Edited spec"
    assert pending.edited_request.acceptance == "Edited acceptance"
    assert pending.edited_request.summary == "Edited summary"


def test_pending_wrong_tool_call_id_does_not_resolve_unrelated_pending():
    pending_map = DispatchPendingMap()
    pending = pending_map.register("call_dispatch", _request())

    resolved = pending_map.resolve_dispatched(
        "call_wrong",
        goal="Edited goal",
        files=["aura/bridge/dispatch.py"],
        spec="Edited spec",
        acceptance="Edited acceptance",
        summary="Edited summary",
    )

    assert resolved is False
    assert not pending.decision_event.is_set()
    assert pending.edited_request is None
    assert pending_map.active_ids() == ["call_dispatch"]


def test_dispatch_session_emits_started_before_worker_step_and_finished_after():
    request = _request()
    step = WorkerStepSpec(
        id="step-1",
        title="Patch bridge handoff",
        goal=request.goal,
        spec=request.spec,
        files=list(request.files),
        acceptance=request.acceptance,
    )
    plan = WorkerDispatchPlan(
        overall_goal=request.goal,
        visible_summary=request.summary,
        global_files=list(request.files),
        steps=[step],
    )
    events: list[tuple] = []
    calls: list[str] = []

    def run_worker_step(tool_call_id, step_req, pending):
        assert events == [("started", "call_dispatch")]
        calls.append(step_req.goal)
        events.append(("worker_step", tool_call_id, step_req.goal, pending))
        return WorkerDispatchResult(
            ok=True,
            summary="Worker completed.",
            status=WorkerOutcomeStatus.completed.value,
            modified_files=list(step_req.files),
        )

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=request,
        plan=plan,
        run_worker_step=run_worker_step,
        pending=SimpleNamespace(),
        event_bus=EventBus(),
        emit_worker_started=lambda tool_id: events.append(("started", tool_id)),
        emit_worker_finished=lambda tool_id, ok, summary, needs_followup, status: events.append(
            ("finished", tool_id, ok, summary, needs_followup, status)
        ),
    )

    result = session.run()

    assert result.ok is True
    assert calls == [request.goal]
    assert events[0] == ("started", "call_dispatch")
    assert events[1][0] == "worker_step"
    assert events[-1] == (
        "finished",
        "call_dispatch",
        True,
        "Worker completed.",
        False,
        WorkerOutcomeStatus.completed.value,
    )
