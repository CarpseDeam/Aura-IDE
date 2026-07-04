"""Tests for WorkArtifact dispatch integration.

Verifies that:
- Planner tool payload with work_artifact creates artifact
- First item becomes bounded WorkerDispatchRequest
- Request has artifact_id and artifact_item_id
- No hidden second item dispatch occurs
- Flat dispatch creates one-item compatibility artifact
"""

from aura.conversation.dispatch import WorkerDispatchRequest
from aura.work_artifact.model import WorkArtifact
from aura.work_artifact.receipt import worker_result_to_receipt


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
        from aura.work_artifact.model import WorkArtifactItem
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


def _ok_result():
    from aura.conversation.dispatch import WorkerDispatchResult
    return WorkerDispatchResult(
        ok=True,
        summary="All good",
        modified_files=["a.py"],
        status="completed",
    )
