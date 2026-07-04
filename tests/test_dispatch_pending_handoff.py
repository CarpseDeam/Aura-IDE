from aura.bridge.dispatch_pending import DispatchPendingMap
from aura.conversation.dispatch import WorkerDispatchRequest


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
