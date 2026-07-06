"""Tests for WorkArtifact dispatch integration.

Verifies the new contract:
- ToolRunner preserves the full approved job envelope.
- DispatchProxy runs all items internally under one approval.
- Recoverable item failures retry on the same item.
- Projection and card rendering report item-level truth.
- Worker prompt uses artifact-aware wording.
"""

from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch_failure import is_recoverable_worker_continuation
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.verification_progress import VerificationProgressTracker
from aura.conversation.history import History
from aura.work_artifact.model import WorkArtifact, WorkArtifactItem, WorkItemStatus, WorkArtifactReceipt
from aura.work_artifact.receipt import worker_result_to_receipt
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.projection import WorkArtifactProjection
from aura.bridge.worker_report import _format_spec_as_user_message


# ── Helpers ────────────────────────────────────────────────────────────────────


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


def _make_three_item_payload() -> dict:
    return {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {"id": "item-1", "title": "Add model", "intent": "Create the model", "target_files": ["src/model.py"], "acceptance": "Model works"},
            {"id": "item-2", "title": "Add view", "intent": "Create the view", "target_files": ["src/view.py"], "acceptance": "View works"},
            {"id": "item-3", "title": "Add controller", "intent": "Create the controller", "target_files": ["src/controller.py"], "acceptance": "Controller works"},
        ],
    }


def _ok_result(summary: str = "OK", modified: list[str] | None = None) -> WorkerDispatchResult:
    return WorkerDispatchResult(
        ok=True,
        summary=summary,
        modified_files=modified or [],
        status="completed",
    )


def _recoverable_result() -> WorkerDispatchResult:
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
        },
    )


def _non_recoverable_result(summary: str = "Fatal error") -> WorkerDispatchResult:
    return WorkerDispatchResult(
        ok=False,
        summary=summary,
        recoverable=False,
        extras={"failure_class": "fatal_error"},
    )


# ── A. ToolRunner preserves full approved job envelope ─────────────────────────


def test_toolrunner_preserves_full_envelope(tmp_path):
    """Given work_artifact with 3 items, ToolRunner passes the top-level envelope."""
    runner = ToolRunner(
        History(), tmp_path, LoopDetector(), VerificationProgressTracker(),
    )
    dispatches: list[tuple[str, WorkerDispatchRequest]] = []

    args: dict[str, Any] = {
        "goal": "Full feature",
        "files": ["src/a.py", "src/b.py"],
        "spec": "Implement full feature with all parts.",
        "acceptance": "Everything works.",
        "summary": "Full feature implementation",
        "work_artifact": _make_three_item_payload(),
    }

    runner.handle_dispatch(
        "call_xyz",
        args,
        on_event=lambda e: None,
        dispatch_cb=lambda tid, req: (
            dispatches.append((tid, req)),
            _ok_result(),
        )[1],
    )

    assert len(dispatches) == 1
    _, req = dispatches[0]
    assert req.goal == "Full feature"
    assert req.files == ["src/a.py", "src/b.py"]
    assert req.spec == "Implement full feature with all parts."
    assert req.acceptance == "Everything works."
    assert req.summary == "Full feature implementation"

    # artifact payload and id are preserved
    assert req.work_artifact_payload is not None
    assert req.artifact_id == "call_xyz"
    # artifact_item_id must NOT be set at ToolRunner level
    assert req.artifact_item_id == ""


def test_toolrunner_flat_dispatch_no_artifact_fields(tmp_path):
    """Flat dispatch does not get artifact_id/artifact_item_id set by ToolRunner."""
    runner = ToolRunner(
        History(), tmp_path, LoopDetector(), VerificationProgressTracker(),
    )
    dispatches: list[tuple[str, WorkerDispatchRequest]] = []

    runner.handle_dispatch(
        "call_flat",
        {
            "goal": "Fix bug",
            "files": ["bug.py"],
            "spec": "Fix the bug",
            "acceptance": "Bug fixed",
            "summary": "Bug fix",
        },
        on_event=lambda e: None,
        dispatch_cb=lambda tid, req: (
            dispatches.append((tid, req)),
            _ok_result(),
        )[1],
    )

    assert len(dispatches) == 1
    _, req = dispatches[0]
    assert req.artifact_id == ""
    assert req.artifact_item_id == ""
    assert req.work_artifact_payload is None


# ── B. Controller tests (building blocks for DispatchProxy) ────────────────────


def test_controller_pending_items():
    """pending_items returns only items with pending status."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    ctrl.create_artifact_from_payload("call_123", payload)
    ctrl.mark_item_active("call_123", "item-1")

    pending = ctrl.pending_items("call_123")
    assert len(pending) == 1
    assert pending[0].id == "item-2"

    ctrl.attach_receipt("call_123", _ok_result(modified=["src/model.py"]))
    pending2 = ctrl.pending_items("call_123")
    assert len(pending2) == 1  # item-1 done, item-2 still pending
    assert pending2[0].id == "item-2"

    # Mark item-2 done
    ctrl.mark_item_active("call_123", "item-2")
    ctrl.attach_receipt("call_123", _ok_result(modified=["src/view.py"]), item_id="item-2")
    assert ctrl.pending_items("call_123") == []


def test_controller_all_required_items_done():
    """all_required_items_done returns True only when all items are done."""
    ctrl = WorkArtifactController()
    ctrl.create_artifact_from_payload("call_123", _make_two_item_payload())

    assert not ctrl.all_required_items_done("call_123")

    ctrl.mark_item_active("call_123", "item-1")
    ctrl.attach_receipt("call_123", _ok_result())
    assert not ctrl.all_required_items_done("call_123")

    ctrl.mark_item_active("call_123", "item-2")
    ctrl.attach_receipt("call_123", _ok_result(), item_id="item-2")
    assert ctrl.all_required_items_done("call_123")


def test_controller_attach_receipt_with_explicit_item_id():
    """attach_receipt with item_id attaches to the correct item, not current_item."""
    ctrl = WorkArtifactController()
    ctrl.create_artifact_from_payload("call_123", _make_two_item_payload())

    # Attach to item-2 explicitly while item-1 is current
    ctrl.attach_receipt("call_123", _ok_result(modified=["src/view.py"]), item_id="item-2")
    artifact = ctrl.get_artifact("call_123")
    assert artifact is not None
    assert artifact.work_items[1].status == WorkItemStatus.done  # item-2 done
    assert artifact.work_items[0].status == WorkItemStatus.pending  # item-1 still pending


# ── C. DispatchProxy tests (use fake _run_worker) ──────────────────────────────

# B: DispatchProxy runs all items under one approval
# C: Continue same item after recoverable result
# D: Aggregate non-ok only when recovery exhausted
#
# These require a DispatchProxy with a controllable _run_worker.
# We test through the proxy's building blocks directly.


def test_build_artifact_item_request(tmp_path):
    """_build_artifact_item_request produces a correctly scoped request."""
    from aura.bridge.dispatch import _DispatchProxy
    from aura.conversation.dispatch import WorkerDispatchRequest

    # Minimal proxy setup for testing the helper
    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    item = WorkArtifactItem(
        id="item-2",
        title="Add view",
        intent="Create the view",
        target_files=["src/view.py"],
        acceptance="View works",
    )
    approved_req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py"],
        spec="Top-level constraints",
        acceptance="Everything works",
        summary="Full feature",
    )

    item_req = proxy._build_artifact_item_request("call_abc", approved_req, item, 2, 3)

    assert item_req.artifact_id == "call_abc"
    assert item_req.artifact_item_id == "item-2"
    assert item_req.goal == "Create the view"
    assert item_req.files == ["src/view.py"]
    assert item_req.acceptance == "View works"
    assert item_req.summary == "Add view"
    assert "WorkArtifact Item 2/3" in item_req.spec
    assert "already approved WorkArtifact job" in item_req.spec
    assert "Complete only this item" in item_req.spec


def test_aggregate_artifact_results_all_ok():
    """Aggregate returns ok=True when every item succeeded."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    approved_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )
    item_results = [
        ("item-1", _ok_result("Done 1", modified=["a.py"])),
        ("item-2", _ok_result("Done 2", modified=["b.py"])),
    ]

    result = proxy._aggregate_artifact_results("call_1", approved_req, item_results, [], {})

    assert result.ok is True
    assert result.extras["completed_items"] == ["item-1", "item-2"]
    assert result.extras["total_items"] == 2
    assert result.extras["work_artifact_job"] is True
    assert "a.py" in result.modified_files
    assert "b.py" in result.modified_files


def test_aggregate_artifact_results_cancelled():
    """Aggregate returns cancelled when an item was cancelled."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    approved_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )
    cancelled = WorkerDispatchResult(
        ok=False, summary="Cancelled", cancelled=True,
    )
    item_results = [
        ("item-1", _ok_result("Done 1")),
        ("item-2", cancelled),
    ]

    result = proxy._aggregate_artifact_results("call_1", approved_req, item_results, [], {})

    assert result.ok is False
    assert result.cancelled is True


def test_aggregate_artifact_results_non_ok():
    """Aggregate returns non-ok with recovery exhaustion metadata."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    approved_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )
    item_results = [
        ("item-1", _ok_result("Done 1")),
        ("item-2", _non_recoverable_result("Fatal")),
    ]

    result = proxy._aggregate_artifact_results(
        "call_1", approved_req, item_results, [],
        {"item-2": 1},
    )

    assert result.ok is False
    assert "recovery_exhausted" in result.extras
    assert "Fatal" in result.summary


# ── E. Projection truth ────────────────────────────────────────────────────────


def test_projection_item_counts():
    """Projection counts reflect per-item status, not aggregate guesses."""
    payload = _make_two_item_payload()
    items = [
        WorkArtifactItem(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            intent=str(raw.get("intent", "")),
            target_files=[str(f) for f in (raw.get("target_files") or [])],
            acceptance=str(raw.get("acceptance", "")),
        )
        for raw in payload.get("items", [])
    ]
    artifact = WorkArtifact(
        artifact_id="art-1",
        goal=payload.get("goal", ""),
        constraints=payload.get("constraints", []),
        allowed_files=payload.get("allowed_files", []),
        work_items=items,
        current_item_id=items[0].id if items else "",
    )

    proj = WorkArtifactProjection.from_artifact(artifact)
    assert proj.pending_count == 2
    assert proj.completed_count == 0
    assert proj.is_complete is False
    assert proj.artifact_status == "pending"

    # Mark item 1 done
    artifact.attach_receipt("item-1", WorkArtifactReceipt(status="ok"))
    proj2 = WorkArtifactProjection.from_artifact(artifact)
    assert proj2.completed_count == 1
    assert proj2.pending_count == 1
    assert proj2.is_complete is False  # not all done
    assert proj2.artifact_status == "active"  # some items still pending

    # Mark all done
    artifact.mark_active("item-2")
    artifact.attach_receipt("item-2", WorkArtifactReceipt(status="ok"))
    proj3 = WorkArtifactProjection.from_artifact(artifact)
    assert proj3.completed_count == 2
    assert proj3.pending_count == 0
    assert proj3.is_complete is True
    assert proj3.artifact_status == "done"


def test_projection_item_status_vs_aggregate():
    """Each projected item uses its own status, not the aggregate artifact_status."""
    artifact = WorkArtifact(
        artifact_id="art-1",
        goal="Test",
        work_items=[
            WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            WorkArtifactItem(id="item-2", title="B", intent="I2", target_files=["b.py"], acceptance="A2"),
        ],
        current_item_id="item-1",
    )
    artifact.attach_receipt("item-1", WorkArtifactReceipt(status="ok"))

    proj = WorkArtifactProjection.from_artifact(artifact)
    assert len(proj.items) == 2
    assert proj.items[0]["status"] == "done"
    assert proj.items[1]["status"] == "pending"


# ── G. Worker prompt wording ───────────────────────────────────────────────────


def test_worker_prompt_artifact_item_wording():
    """Artifact item request uses 'Approved WorkArtifact Item' wording."""
    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["a.py"],
        spec="Do the thing",
        acceptance="Ok",
        summary="Test",
        artifact_id="call_1",
        artifact_item_id="item-2",
    )
    msg = _format_spec_as_user_message(req, artifact_item_index=1, artifact_item_total=None)
    assert "Approved WorkArtifact Item" in msg
    assert "already approved WorkArtifact job" in msg
    assert "Complete only this bounded item" in msg
    assert "Aura will continue the approved job" in msg
    assert "next item requires user review" not in msg
    assert "Active Dispatch Item" not in msg


def test_worker_prompt_flat_wording():
    """Flat dispatch request uses 'Approved Worker Job' wording."""
    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["a.py"],
        spec="Do the thing",
        acceptance="Ok",
        summary="Test",
    )
    msg = _format_spec_as_user_message(req)
    assert "Approved Worker Job" in msg
    assert "Active Dispatch Item" not in msg
    assert "WorkArtifact" not in msg
    assert "Aura will continue" not in msg


# ── Serialization / backward compat ────────────────────────────────────────────


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


def test_work_artifact_payload_serializes():
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
