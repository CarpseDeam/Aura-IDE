"""Tests for WorkArtifact dispatch integration.

Verifies that:
- Planner tool payload with work_artifact creates artifact
- First item becomes bounded WorkerDispatchRequest
- Request has artifact_id and artifact_item_id
- No hidden second item dispatch occurs
- Flat dispatch creates one-item compatibility artifact
- Multi-item artifact preserves all items through controller
- Projection callback fires on all mutation events
- WorkerDispatchRequest serializes/deserializes work_artifact_payload
"""

from aura.conversation.dispatch import WorkerDispatchRequest
from aura.work_artifact.model import WorkArtifact, WorkArtifactItem, WorkItemStatus
from aura.work_artifact.receipt import worker_result_to_receipt
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.projection import WorkArtifactProjection


def test_artifact_id_and_item_id_on_request():
    """A dispatch request with artifact_id and artifact_item_id serializes correctly."""
    req = WorkerDispatchRequest(
        goal="Test",
        files=["a.py"],
        spec="Do the thing",
        acceptance="Works",
        summary="Test",
        artifact_id="art-1",
        artifact_item_id="item-1",
    )
    d = req.to_dict()
    assert d.get("artifact_id") == "art-1"
    assert d.get("artifact_item_id") == "item-1"

    restored = WorkerDispatchRequest.from_dict(d)
    assert restored.artifact_id == "art-1"
    assert restored.artifact_item_id == "item-1"


def test_flat_request_does_not_have_steps():
    """A flat dispatch request should not contain a 'steps' field."""
    req = WorkerDispatchRequest(
        goal="Test",
        files=["a.py"],
        spec="Do the thing",
        acceptance="Works",
        summary="Test",
    )
    d = req.to_dict()
    assert "steps" not in d


def test_work_artifact_from_payload():
    """Test creating a WorkArtifact from a tool payload shape."""
    payload = {
        "goal": "Implement feature",
        "constraints": ["No new deps"],
        "allowed_files": ["src/"],
        "items": [
            {"id": "item-1", "title": "Add model", "intent": "Create the model", "target_files": ["src/model.py"], "acceptance": "Model works"},
        ],
    }
    items = []
    for raw in payload.get("items", []):
        items.append(WorkArtifactItem(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            intent=str(raw.get("intent", "")),
            target_files=[str(f) for f in (raw.get("target_files") or [])],
            acceptance=str(raw.get("acceptance", "")),
        ))
    artifact = WorkArtifact(
        artifact_id="call_123",
        goal=str(payload.get("goal", "")),
        constraints=[str(c) for c in (payload.get("constraints") or [])],
        allowed_files=[str(f) for f in (payload.get("allowed_files") or [])],
        work_items=items,
        current_item_id=items[0].id if items else "",
    )
    assert artifact.artifact_id == "call_123"
    assert len(artifact.work_items) == 1
    assert artifact.current_item_id == "item-1"
    item = artifact.current_item()
    assert item is not None
    assert item.title == "Add model"
    assert item.intent == "Create the model"


def test_worker_result_to_receipt_ok():
    """A successful Worker result becomes an 'ok' receipt."""
    result = _ok_result()
    receipt = worker_result_to_receipt(result)
    assert receipt.status == "ok"
    assert receipt.summary == "All good"
    assert receipt.modified_files == ["a.py"]


def test_worker_result_to_receipt_failed():
    """A failed Worker result becomes a 'blocked' receipt."""
    from aura.conversation.dispatch import WorkerDispatchResult
    result = WorkerDispatchResult(
        ok=False,
        summary="Something broke",
        modified_files=[],
    )
    receipt = worker_result_to_receipt(result)
    assert receipt.status == "blocked"


def test_worker_result_to_receipt_cancelled():
    """A cancelled Worker result becomes a 'cancelled' receipt."""
    from aura.conversation.dispatch import WorkerDispatchResult
    result = WorkerDispatchResult(
        ok=False,
        summary="Cancelled",
        cancelled=True,
    )
    receipt = worker_result_to_receipt(result)
    assert receipt.status == "cancelled"


def test_worker_result_to_receipt_recoverable_continuation():
    """A recoverable phase-boundary dispatch result becomes a continuing receipt."""
    result = _recoverable_quality_continuation_result()
    receipt = worker_result_to_receipt(result)
    assert receipt.status == "continuing"
    assert receipt.metadata["failure_class"] == "worker_quality_unresolved_findings"


def test_recoverable_quality_payload_does_not_mark_artifact_item_blocked():
    """The exact worker_quality_unresolved_findings payload keeps the item active."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    ctrl.create_artifact_from_payload("call_123", payload)
    ctrl.mark_item_active("call_123", "item-1")

    ctrl.attach_receipt("call_123", _recoverable_quality_continuation_result())

    artifact = ctrl.get_artifact("call_123")
    assert artifact is not None
    item = artifact.work_items[0]
    assert item.status == WorkItemStatus.active
    assert item.receipt is not None
    assert item.receipt.status == "continuing"

    projection = WorkArtifactProjection.from_artifact(artifact)
    assert projection.active_count == 1
    assert projection.blocked_count == 0
    assert projection.pending_count == 1
    assert not projection.is_complete


# ── Multi-item artifact tests ────────────────────────────────────────────────


def _make_two_item_payload() -> dict:
    return {
        "goal": "Implement feature",
        "constraints": ["No new deps"],
        "allowed_files": ["src/"],
        "items": [
            {"id": "item-1", "title": "Add model", "intent": "Create the model", "target_files": ["src/model.py"], "acceptance": "Model works"},
            {"id": "item-2", "title": "Add view", "intent": "Create the view", "target_files": ["src/view.py"], "acceptance": "View works"},
        ],
    }


def test_multi_item_artifact_created_with_both_items():
    """Controller.create_artifact_from_payload creates artifact with all items."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    artifact = ctrl.create_artifact_from_payload("call_123", payload)

    assert len(artifact.work_items) == 2
    assert artifact.current_item_id == "item-1"
    assert artifact.current_item() is not None
    assert artifact.current_item().id == "item-1"
    assert artifact.work_items[1].id == "item-2"
    assert artifact.work_items[1].status == WorkItemStatus.pending


def test_multi_item_artifact_preserved_after_receipt():
    """After attaching receipt to item 1, item 2 still exists and advance makes it current."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    ctrl.create_artifact_from_payload("call_123", payload)

    # Simulate Worker completion for item 1
    from aura.conversation.dispatch import WorkerDispatchResult
    result = WorkerDispatchResult(ok=True, summary="Done", modified_files=["src/model.py"])
    ctrl.attach_receipt("call_123", result)

    artifact = ctrl.get_artifact("call_123")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status == WorkItemStatus.done
    assert item1.receipt is not None
    assert item1.receipt.status == "ok"

    # Item 2 should still exist as pending
    assert len(artifact.work_items) == 2
    assert artifact.work_items[1].id == "item-2"
    assert artifact.work_items[1].status == WorkItemStatus.pending

    # Advance to make item 2 current
    has_next = ctrl.advance_to_next_item("call_123")
    assert has_next is True

    artifact = ctrl.get_artifact("call_123")
    assert artifact.current_item_id == "item-2"
    assert artifact.current_item().id == "item-2"


def test_no_auto_dispatch_on_advance():
    """advance_to_next_item returns True for next item but does not dispatch Worker."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    ctrl.create_artifact_from_payload("call_123", payload)

    from aura.conversation.dispatch import WorkerDispatchResult
    result = WorkerDispatchResult(ok=True, summary="Done")
    ctrl.attach_receipt("call_123", result)

    # advance_to_next_item should return True and set current to item 2
    has_next = ctrl.advance_to_next_item("call_123")
    assert has_next is True

    # The current item should be item 2 (still pending, not active)
    artifact = ctrl.get_artifact("call_123")
    assert artifact.current_item().status == WorkItemStatus.pending


def test_complete_artifact_advance_returns_false():
    """advance_to_next_item returns False when all items are done."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    ctrl.create_artifact_from_payload("call_123", payload)

    from aura.conversation.dispatch import WorkerDispatchResult

    # Complete item 1
    ctrl.attach_receipt("call_123", WorkerDispatchResult(ok=True, summary="Done"))
    ctrl.advance_to_next_item("call_123")

    # Complete item 2
    ctrl.mark_item_active("call_123", "item-2")
    ctrl.attach_receipt("call_123", WorkerDispatchResult(ok=True, summary="Done"))

    # No more items
    has_next = ctrl.advance_to_next_item("call_123")
    assert has_next is False


def test_projection_callback_fires_on_mutations():
    """Projection callback fires on create, mark_active, attach_receipt, advance."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    calls = []

    ctrl.set_on_projection_updated(lambda p: calls.append(p))

    # create
    ctrl.create_artifact_from_payload("call_123", payload)
    assert len(calls) == 1
    assert calls[-1].artifact_id == "call_123"
    assert len(calls[-1].items) == 2

    # mark_active
    ctrl.mark_item_active("call_123", "item-1")
    assert len(calls) == 2

    # attach_receipt
    from aura.conversation.dispatch import WorkerDispatchResult
    ctrl.attach_receipt("call_123", WorkerDispatchResult(ok=True, summary="Done"))
    assert len(calls) == 3
    assert calls[-1].completed_count == 1

    # advance
    ctrl.advance_to_next_item("call_123")
    assert len(calls) == 4
    assert calls[-1].current_item_id == "item-2"


def test_one_item_compatibility_artifact():
    """Flat dispatch creates one-item compatibility artifact through controller."""
    ctrl = WorkArtifactController()
    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["a.py"],
        spec="Do it",
        acceptance="Works",
        summary="Test",
        artifact_id="call_123",
        artifact_item_id="item-1",
    )
    artifact = ctrl.create_one_item_artifact("call_123", req)

    assert len(artifact.work_items) == 1
    assert artifact.work_items[0].id == "item-1"
    assert artifact.work_items[0].status == WorkItemStatus.pending


def test_current_dispatch_request_returns_correct_item():
    """current_dispatch_request returns bounded request for the current artifact item."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    ctrl.create_artifact_from_payload("call_123", payload)

    original_req = WorkerDispatchRequest(
        goal="Original goal",
        files=[],
        spec="Spec",
        acceptance="Accept",
        summary="Summary",
    )
    bounded = ctrl.current_dispatch_request("call_123", original_req)
    assert bounded is not None
    assert bounded.artifact_item_id == "item-1"
    assert bounded.goal == "Create the model"  # item-1 intent

    # After advance, current_dispatch_request should return item 2
    from aura.conversation.dispatch import WorkerDispatchResult
    ctrl.attach_receipt("call_123", WorkerDispatchResult(ok=True, summary="Done"))
    ctrl.advance_to_next_item("call_123")

    bounded2 = ctrl.current_dispatch_request("call_123", original_req)
    assert bounded2 is not None
    assert bounded2.artifact_item_id == "item-2"
    assert bounded2.goal == "Create the view"


def test_work_artifact_payload_serializes_through_worker_dispatch_request():
    """work_artifact_payload survives to_dict/from_dict round-trip."""
    payload = {
        "goal": "Test",
        "constraints": [],
        "allowed_files": [],
        "items": [
            {"id": "i1", "title": "Item 1", "intent": "Do 1", "target_files": ["a.py"], "acceptance": "OK"},
            {"id": "i2", "title": "Item 2", "intent": "Do 2", "target_files": ["b.py"], "acceptance": "OK"},
        ],
    }
    req = WorkerDispatchRequest(
        goal="G",
        files=[],
        spec="S",
        acceptance="A",
        summary="Sum",
        work_artifact_payload=payload,
    )
    d = req.to_dict()
    assert d.get("work_artifact_payload") == payload

    restored = WorkerDispatchRequest.from_dict(d)
    assert restored.work_artifact_payload == payload


def test_work_artifact_payload_not_in_flat_request():
    """Flat dispatch request does not contain work_artifact_payload."""
    req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )
    d = req.to_dict()
    assert "work_artifact_payload" not in d


def test_missing_artifact_returns_none():
    """Controller methods return None gracefully for missing artifact IDs."""
    ctrl = WorkArtifactController()
    from aura.conversation.dispatch import WorkerDispatchResult

    assert ctrl.get_artifact("nonexistent") is None
    assert ctrl.advance_to_next_item("nonexistent") is False
    assert ctrl.has_more_items("nonexistent") is False
    # These should not raise
    ctrl.mark_item_active("nonexistent", "item-1")
    ctrl.attach_receipt("nonexistent", WorkerDispatchResult(ok=True, summary="OK"))
    ctrl.remove_artifact("nonexistent")
    ctrl.clear()


def test_projection_from_multi_item_artifact():
    """WorkArtifactProjection.from_artifact reflects item status counts."""
    payload = _make_two_item_payload()
    items = []
    for raw in payload.get("items", []):
        items.append(WorkArtifactItem(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            intent=str(raw.get("intent", "")),
            target_files=[str(f) for f in (raw.get("target_files") or [])],
            acceptance=str(raw.get("acceptance", "")),
        ))
    artifact = WorkArtifact(
        artifact_id="call_123",
        goal=payload.get("goal", ""),
        constraints=payload.get("constraints", []),
        allowed_files=payload.get("allowed_files", []),
        work_items=items,
        current_item_id=items[0].id if items else "",
    )

    proj = WorkArtifactProjection.from_artifact(artifact)
    assert proj.artifact_id == "call_123"
    assert len(proj.items) == 2
    assert proj.current_item_id == "item-1"
    assert proj.pending_count == 2
    assert proj.completed_count == 0
    assert not proj.is_complete

    # Mark item 1 done
    from aura.work_artifact.model import WorkArtifactReceipt
    artifact.attach_receipt("item-1", WorkArtifactReceipt(status="ok"))
    proj2 = WorkArtifactProjection.from_artifact(artifact)
    assert proj2.completed_count == 1
    assert proj2.pending_count == 1
    assert not proj2.is_complete

    # Advance and mark item 2 done
    artifact.advance()
    artifact.attach_receipt("item-2", WorkArtifactReceipt(status="ok"))
    proj3 = WorkArtifactProjection.from_artifact(artifact)
    assert proj3.completed_count == 2
    assert proj3.pending_count == 0
    assert proj3.is_complete


def test_projection_with_active_only_item_is_not_complete():
    """Active/continuing items must not make the artifact complete."""
    artifact = WorkArtifact(
        artifact_id="call_123",
        goal="Test",
        work_items=[
            WorkArtifactItem(
                id="item-1",
                title="Item 1",
                intent="Do 1",
                target_files=["a.py"],
                acceptance="OK",
                status=WorkItemStatus.active,
            ),
        ],
        current_item_id="item-1",
    )

    projection = WorkArtifactProjection.from_artifact(artifact)
    assert projection.pending_count == 0
    assert projection.active_count == 1
    assert not projection.is_complete


def _ok_result():
    from aura.conversation.dispatch import WorkerDispatchResult
    return WorkerDispatchResult(
        ok=True,
        summary="All good",
        modified_files=["a.py"],
        status="completed",
    )


def _recoverable_quality_continuation_result():
    from aura.conversation.dispatch import WorkerDispatchResult

    return WorkerDispatchResult(
        ok=False,
        summary="Worker quality findings are recoverable.",
        recoverable=True,
        phase_boundary=True,
        needs_followup=True,
        extras={
            "failure_class": "worker_quality_unresolved_findings",
            "recoverable": True,
            "phase_boundary": True,
            "suggested_next_tool": "dispatch_to_worker",
            "details": {
                "recoverable": True,
                "phase_boundary": True,
                "suggested_next_tool": "dispatch_to_worker",
                "findings": [
                    {
                        "kind": "large_diff_whole_file_rewrite",
                        "severity": "warning",
                        "file": "a.py",
                    }
                ],
            },
        },
    )
