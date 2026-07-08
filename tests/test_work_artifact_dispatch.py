"""Tests for WorkArtifact dispatch integration.

Verifies the new contract:
- ToolRunner preserves the full approved job envelope.
- DispatchProxy runs all items internally under one approval.
- Recoverable item failures retry on the same item.
- Projection and card rendering report item-level truth.
- Worker prompt uses artifact-aware wording.
"""

from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult, WorkerMismatch
from aura.conversation.dispatch_failure import is_recoverable_worker_continuation
from aura.conversation.tool_runner import ToolRunner
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


def _continuing_with_evidence_result(
    summary: str = "Item completed — acceptance unverified, continuing.",
    modified: list[str] | None = None,
) -> WorkerDispatchResult:
    """Simulate a result where the Worker did real work (files changed,
    validation passed) but the outcome classifier emitted a recoverable/
    continuing/phase_boundary classification.

    This is the exact pattern that caused the "corpse state" bug: the
    Worker succeeded but receipt.status was "continuing" because
    ``is_recoverable_worker_continuation`` matched.
    """
    return WorkerDispatchResult(
        ok=False,
        summary=summary,
        recoverable=True,
        phase_boundary=True,
        needs_followup=True,
        status="completed_with_caveats",
        modified_files=modified or [],
        validation="python -m compileall src/model.py passed.",
        extras={
            "recoverable": True,
            "phase_boundary": True,
            "needs_followup": True,
            "suggested_next_tool": "dispatch_to_worker",
            "failure_class": "behavioral_validation_skipped",
        },
    )


# ── A. ToolRunner preserves full approved job envelope ─────────────────────────


def test_toolrunner_preserves_full_envelope(tmp_path):
    """Given work_artifact with 3 items, ToolRunner passes the top-level envelope."""
    runner = ToolRunner(
        History(), tmp_path,
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
        History(), tmp_path,
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
    """pending_items returns unfinished items (not done)."""
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    ctrl.create_artifact_from_payload("call_123", payload)
    ctrl.mark_item_active("call_123", "item-1")

    pending = ctrl.pending_items("call_123")
    assert len(pending) == 2  # item-1 active, item-2 pending — both unfinished
    assert pending[0].id == "item-1"
    assert pending[1].id == "item-2"

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


# ── continuation: empty validation commands do not block item done ────────


def test_artifact_continues_after_item_with_empty_validation_commands():
    """A WorkArtifact item with empty validation commands can still complete
    and the artifact continues to the next pending item.

    Regression: empty or misdeclared validation commands must never prevent
    a completed item from being marked done, which would stall the whole
    multi-item WorkArtifact job.
    """
    from aura.work_artifact.model import ValidationCommandSpec

    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    # Add an empty validation command to item-1 (simulating a declared but
    # empty acceptance validation command).
    payload["items"][0]["validation_commands"] = [
        ValidationCommandSpec(command="").to_dict(),
    ]
    ctrl.create_artifact_from_payload("call_cont", payload)

    # Run item-1: mark active, attach ok result.
    ctrl.mark_item_active("call_cont", "item-1")
    ctrl.attach_receipt("call_cont", _ok_result(modified=["src/model.py"]))

    # Item-1 must be done even though it had an empty validation command.
    artifact = ctrl.get_artifact("call_cont")
    assert artifact is not None
    assert artifact.work_items[0].status == WorkItemStatus.done
    assert artifact.work_items[0].receipt is not None
    assert artifact.work_items[0].receipt.status == "ok"

    # The artifact must have item-2 as the next pending item.
    pending = ctrl.pending_items("call_cont")
    assert len(pending) == 1
    assert pending[0].id == "item-2"


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

    result = proxy._aggregate_artifact_results("call_1", approved_req, item_results, [], {}, 2)

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

    result = proxy._aggregate_artifact_results("call_1", approved_req, item_results, [], {}, 2)

    assert result.ok is False
    assert result.cancelled is True
    assert result.extras["current_item_id"] == "item-2"
    assert result.extras["total_items"] == 2


def test_aggregate_artifact_results_non_ok():
    """Aggregate returns non-ok with correct truth — 3 items, item-2 failed, item-3 never run."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    approved_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )
    # 3-item artifact: item-1 ok, item-2 fails, item-3 never started
    item_results = [
        ("item-1", _ok_result("Done 1")),
        ("item-2", _non_recoverable_result("Fatal")),
    ]

    result = proxy._aggregate_artifact_results(
        "call_1", approved_req, item_results, [],
        {"item-2": 1}, 3,
    )

    assert result.ok is False
    assert result.extras["total_items"] == 3
    assert result.extras["completed_items"] == ["item-1"]
    assert result.extras["failed_item_id"] == "item-2"
    assert result.extras["current_item_id"] == "item-2"
    assert result.extras.get("recovery_exhausted") is not True
    assert "item-2" in result.summary
    assert "Fatal" in result.summary


# ── C/D. request_dispatch integration ─────────────────────────────────────────

def test_request_dispatch_work_artifact_runs_item_one_as_bounded_request(tmp_path):
    """Item 1 receives a bounded item request, not the full approved job.

    Regression: item 1 must NOT run on the top-level approved request.
    Every item, including item 1, must be a bounded WorkerDispatchRequest
    scoped to that item's target files.
    """
    from aura.bridge.dispatch import _DispatchProxy

    captured: list[WorkerDispatchRequest] = []

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py", "src/b.py"],
        spec="Implement the full feature with all parts.",
        acceptance="Everything works.",
        summary="Full feature implementation",
        work_artifact_payload=payload,
    )

    # Capture every _run_worker request
    def capturing_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = capturing_run_worker

    # Bypass Qt signal: auto-resolve the pending dispatch immediately
    original_register = proxy._pending_map.register
    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    result = proxy.request_dispatch("call_regression", req)

    assert result.ok is True
    assert len(captured) == 2

    # Item 1 is a bounded request scoped to its target files
    r1 = captured[0]
    assert r1.artifact_item_id == "item-1"
    assert r1.files == ["src/model.py"], f"Expected item-1 files [src/model.py], got {r1.files}"
    assert "WorkArtifact Item 1/2" in r1.spec
    assert "Complete only this item" in r1.spec

    # Item 2 is a bounded request scoped to its target files
    r2 = captured[1]
    assert r2.artifact_item_id == "item-2"
    assert r2.files == ["src/view.py"], f"Expected item-2 files [src/view.py], got {r2.files}"

    # No captured request uses the top-level files or has empty artifact_item_id
    for r in captured:
        assert r.files != ["src/a.py", "src/b.py"], (
            f"Item {r.artifact_item_id} used top-level files instead of item files"
        )
        assert r.artifact_item_id != "", "Item has empty artifact_item_id"

    # Both artifact items are internally done
    ctrl = proxy.artifact_controller()
    assert ctrl.all_required_items_done("call_regression")


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


# ── H. Four-outcome aggregate ──────────────────────────────────────────────────


def test_aggregate_artifact_results_infrastructure_pause():
    """Infrastructure pause returns recoverable result with work_artifact_unfinished."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    approved_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )
    harness_result = WorkerDispatchResult(
        ok=False, summary="API error: 503 Service Unavailable",
        status="harness_error",
        extras={"api_errors": ["timeout"], "failure_class": "provider_unavailable"},
    )
    item_results = [
        ("item-1", _ok_result("Done 1")),
        ("item-2", harness_result),
    ]

    result = proxy._aggregate_artifact_results(
        "call_1", approved_req, item_results, [], {}, 2,
        terminal_override=harness_result, infrastructure_pause=True,
    )

    assert result.ok is False
    assert result.cancelled is False
    assert result.recoverable is True
    assert result.extras["work_artifact_unfinished"] is True
    assert "pending_item_ids" in result.extras
    assert result.extras["total_items"] == 2
    assert result.extras["completed_items"] == ["item-1"]
    assert "resume" in result.summary.lower() or "reachable" in result.summary.lower()


def test_aggregate_artifact_results_exhausted():
    """Exhausted result uses new summary with per-item ✓/✗ detail."""
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
        ("item-2", _non_recoverable_result("Stalled after syntax retry")),
    ]

    result = proxy._aggregate_artifact_results(
        "call_1", approved_req, item_results, [],
        {"item-2": 3}, 2,
    )

    assert result.ok is False
    assert result.extras.get("recovery_exhausted") is not True
    assert "incomplete" in result.summary.lower()
    assert "✓ item-1" in result.summary
    assert "✗ item-2" in result.summary
    assert "Stalled" in result.summary


# ── I. Infrastructure failure detection ────────────────────────────────────────


def test_is_infrastructure_failure_harness_error():
    """harness_error status is classified as infrastructure failure."""
    from aura.bridge.dispatch import _DispatchProxy
    result = WorkerDispatchResult(ok=False, summary="Err", status="harness_error")
    assert _DispatchProxy._is_infrastructure_failure(result) is True


def test_is_infrastructure_failure_api_errors():
    """api_errors in extras is infrastructure failure."""
    from aura.bridge.dispatch import _DispatchProxy
    result = WorkerDispatchResult(
        ok=False, summary="Err",
        extras={"api_errors": ["timeout"]},
    )
    assert _DispatchProxy._is_infrastructure_failure(result) is True


def test_is_infrastructure_failure_failure_class():
    """provider/network/auth failure classes are infrastructure."""
    from aura.bridge.dispatch import _DispatchProxy
    for fc in ("provider_unavailable", "network_error", "auth_error"):
        result = WorkerDispatchResult(
            ok=False, summary="Err",
            extras={"failure_class": fc},
        )
        assert _DispatchProxy._is_infrastructure_failure(result) is True


def test_is_infrastructure_failure_regular_failure():
    """A regular non-ok worker result is NOT infrastructure."""
    from aura.bridge.dispatch import _DispatchProxy
    result = WorkerDispatchResult(
        ok=False, summary="Validation failed",
        status="validation_failed",
    )
    assert _DispatchProxy._is_infrastructure_failure(result) is False


# ── K. _build_artifact_item_request prior-receipt appendix ─────────────────────


def test_build_artifact_item_request_with_prior_receipt():
    """Item with non-ok receipt gets prior-attempt context in spec."""
    from aura.bridge.dispatch import _DispatchProxy
    from aura.work_artifact.model import WorkArtifactItem, WorkArtifactReceipt

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    item = WorkArtifactItem(
        id="item-2",
        title="Add view",
        intent="Create the view",
        target_files=["src/view.py"],
        acceptance="View works",
        receipt=WorkArtifactReceipt(
            status="failed",
            summary="Failed due to syntax error",
            modified_files=["src/view.py"],
        ),
    )
    approved_req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py"],
        spec="Top-level constraints",
        acceptance="Everything works",
        summary="Full feature",
    )

    item_req = proxy._build_artifact_item_request("call_abc", approved_req, item, 2, 3)

    assert "Previous attempt on this item" in item_req.spec
    assert "failed" in item_req.spec
    assert "syntax error" in item_req.spec
    assert "src/view.py" in item_req.spec


def test_build_artifact_item_request_ok_receipt_skips_appendix():
    """Item with OK receipt does NOT get prior-attempt context."""
    from aura.bridge.dispatch import _DispatchProxy
    from aura.work_artifact.model import WorkArtifactItem, WorkArtifactReceipt

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    item = WorkArtifactItem(
        id="item-1",
        title="Add model",
        intent="Create the model",
        target_files=["src/model.py"],
        acceptance="Model works",
        receipt=WorkArtifactReceipt(status="ok", summary="Done"),
    )
    approved_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )

    item_req = proxy._build_artifact_item_request("call", approved_req, item, 1, 2)

    assert "Previous attempt" not in item_req.spec


def test_build_artifact_item_request_continuing_receipt_skips_appendix():
    """Item with continuing receipt does NOT get prior-attempt context."""
    from aura.bridge.dispatch import _DispatchProxy
    from aura.work_artifact.model import WorkArtifactItem, WorkArtifactReceipt

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    item = WorkArtifactItem(
        id="item-1",
        title="Long task",
        intent="Do the thing",
        target_files=["src/t.py"],
        acceptance="Works",
        receipt=WorkArtifactReceipt(status="continuing", summary="In progress"),
    )
    approved_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Sum",
    )

    item_req = proxy._build_artifact_item_request("call", approved_req, item, 1, 1)

    assert "Previous attempt" not in item_req.spec


# ── L. Resume binding guard ────────────────────────────────────────────────────


def test_request_dispatch_resumes_paused_artifact_job(tmp_path):
    """Binding guard resumes a paused artifact job under the original id.

    The incoming dispatch triggers the guard, which redirects to the
    original tool_call_id and runs the remaining items. The result
    carries ``resumed_artifact_id`` so the Planner can correlate.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    # ── Set up a paused artifact job with pending items ──
    payload = _make_two_item_payload()
    original_id = "call_original"
    original_req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py", "src/b.py"],
        spec="Implement the full feature with all parts.",
        acceptance="Everything works.",
        summary="Full feature implementation",
        work_artifact_payload=payload,
    )

    # Register the artifact and approved request manually (simulating a
    # prior infrastructure pause that left the job in progress).
    proxy._register_artifact_from_request(original_id, original_req)
    proxy._approved_artifact_requests[original_id] = original_req

    # ── Incoming dispatch triggers the binding guard ──
    captured: list[WorkerDispatchRequest] = []

    def capturing_run_worker(tid: str, req: WorkerDispatchRequest, pending) -> WorkerDispatchResult:
        captured.append(req)
        return _ok_result(modified=list(req.files))
    proxy._run_worker = capturing_run_worker

    incoming_id = "call_new"
    incoming_req = WorkerDispatchRequest(
        goal="G", files=[], spec="S", acceptance="A", summary="Resume",
    )

    result = proxy.request_dispatch(incoming_id, incoming_req)

    # The result is tagged with the resumed artifact id.
    assert result.ok is True
    assert result.extras.get("resumed_artifact_id") == original_id

    # Both items were run as bounded requests (no top-level request).
    assert len(captured) == 2
    for r in captured:
        assert r.artifact_item_id != ""
        assert "WorkArtifact Item" in r.spec

    # Artifact under the original id is fully completed; the incoming id
    # has no artifact (it was cleaned up by the resume path).
    ctrl = proxy.artifact_controller()
    assert ctrl.get_artifact(incoming_id) is None
    assert ctrl.all_required_items_done(original_id)


def test_resume_guard_does_not_fire_on_first_dispatch(tmp_path):
    """Binding guard does NOT fire when no approved request has pending items."""
    from aura.bridge.dispatch import _DispatchProxy

    captured: list[WorkerDispatchRequest] = []

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    def capturing_run_worker(tid, req, pending):
        captured.append(req)
        return _ok_result(modified=list(req.files))
    proxy._run_worker = capturing_run_worker

    # Bypass Qt signal: auto-resolve the pending dispatch immediately.
    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py"],
        spec="Implement the full feature.",
        acceptance="Works.",
        summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_first", req)

    assert result.ok is True
    assert "resumed_artifact_id" not in (result.extras or {})
    assert len(captured) == 2


# ── M. Retry cap pauses job immediately ─────────────────────────────────────────


def test_artifact_retry_cap_pauses_job_immediately(tmp_path):
    """Retry cap on item 2 pauses the job — item 3 never runs.

    Multi-item WorkArtifact: item 1 succeeds, item 2 hits the physical
    retry cap. The job returns a recoverable ``WorkerDispatchResult``
    with ``artifact_retry_cap_reached``, ``work_artifact_unfinished``,
    and NO ``recovery_exhausted``. Item 3 is never dispatched.
    """
    from aura.bridge.dispatch import _DispatchProxy, _ARTIFACT_ITEM_RETRY_CAP

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    # Inject a controllable _run_worker:
    #   item-1 → ok
    #   item-2 → always fails (triggers retry loop → cap)
    #   item-3 → should never be called
    def failing_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-2":
            return _non_recoverable_result(f"Item 2 failure")
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = failing_run_worker

    # Bypass Qt signal: auto-resolve the pending dispatch immediately.
    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_three_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.",
        summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_retry_cap", req)

    # ── Item-2 should have been attempted exactly _ARTIFACT_ITEM_RETRY_CAP times ──
    item2_attempts = sum(
        1 for r in captured if r.artifact_item_id == "item-2"
    )
    assert item2_attempts == _ARTIFACT_ITEM_RETRY_CAP, (
        f"Expected {_ARTIFACT_ITEM_RETRY_CAP} item-2 attempts, got {item2_attempts}"
    )

    # ── Only item-1 and item-2 were dispatched — item-3 never runs ────────────
    # item-1 runs once, item-2 runs _ARTIFACT_ITEM_RETRY_CAP times
    assert len(captured) == 1 + _ARTIFACT_ITEM_RETRY_CAP, (
        f"Expected {1 + _ARTIFACT_ITEM_RETRY_CAP} dispatches "
        f"(item-1 × 1 + item-2 × {_ARTIFACT_ITEM_RETRY_CAP}), got {len(captured)}"
    )

    item3_dispatched = any(
        r.artifact_item_id == "item-3" for r in captured
    )
    assert not item3_dispatched, (
        "Item 3 was dispatched — job should have paused after item-2 retry cap"
    )

    # ── Result is recoverable with retry-cap extras ───────────────────────────
    assert result.recoverable is True, (
        f"Expected recoverable=True, got {result.recoverable}"
    )
    extras = result.extras if isinstance(result.extras, dict) else {}
    assert extras.get("artifact_retry_cap_reached") is True, (
        f"Expected artifact_retry_cap_reached=True, got {extras.get('artifact_retry_cap_reached')}"
    )
    assert extras.get("work_artifact_unfinished") is True, (
        f"Expected work_artifact_unfinished=True, got {extras.get('work_artifact_unfinished')}"
    )
    assert extras.get("recovery_exhausted") is not True, (
        "Must not contain recovery_exhausted"
    )

    # ── Summary says incomplete, not exhausted ────────────────────────────────
    assert "incomplete" in result.summary.lower(), (
        f"Summary should say 'incomplete', got: {result.summary}"
    )
    assert "item-2" in result.summary, (
        f"Summary should mention the failed item, got: {result.summary}"
    )
    assert "recovery exhausted" not in result.summary.lower(), (
        "Summary must not mention 'recovery exhausted'"
    )


# ── N. WorkArtifact item completion normalizer ───────────────────────────────


def test_artifact_normalizer_live_failure_reproduction(tmp_path):
    """Test A: Exact live failure reproduction.

    A two-item WorkArtifact where item 1 returns a recoverable/continuing/
    phase_boundary result BUT with modified files and passing compileall.
    After the normalizer and attach_receipt:

      receipt.status == "ok"
      item 1 status == done
      projection counts: 1 done, 0 active, 1 pending
      item 2 starts automatically
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    # Item 1 returns a continuing-but-successful result; item 2 is normal ok.
    def continuing_then_ok(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            return _continuing_with_evidence_result(
                summary="Item 1 done — no blockers.",
                modified=list(worker_req.files),
            )
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = continuing_then_ok

    # Bypass Qt signal: auto-resolve the pending dispatch immediately.
    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.",
        summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_live_failure", req)

    # ── Both items completed ───────────────────────────────────────────────
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    assert result.extras["completed_items"] == ["item-1", "item-2"]
    assert result.extras["total_items"] == 2
    assert len(captured) == 2, f"Expected 2 dispatches, got {len(captured)}"

    # ── Item 1 receipt is "ok" (not "continuing") ──────────────────────────
    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_live_failure")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.id == "item-1"
    assert item1.status == WorkItemStatus.done, (
        f"Item 1 expected done, got {item1.status}"
    )
    assert item1.receipt is not None
    assert item1.receipt.status == "ok", (
        f"Item 1 receipt expected 'ok', got '{item1.receipt.status}'"
    )

    # ── Projection: 2 done, 0 active, 0 pending ────────────────────────────
    proj = WorkArtifactProjection.from_artifact(artifact)
    assert proj.completed_count == 2, (
        f"Expected 2 completed, got {proj.completed_count}"
    )
    assert proj.pending_count == 0, (
        f"Expected 0 pending, got {proj.pending_count}"
    )


def test_artifact_normalizer_real_failure_not_normalized(tmp_path):
    """Test B: Real validation failure must NOT be normalized.

    When compileall fails (traceback present), the result must stay non-ok.
    The item must NOT become done. Retry/pause behavior is preserved.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def failing_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        # Simulate a compileall failure with traceback
        return WorkerDispatchResult(
            ok=False,
            summary="Item failed: validation error — syntax error in src/model.py",
            status="validation_failed",
            modified_files=list(worker_req.files),
            validation="python -m compileall src/model.py failed with traceback.",
            extras={
                "failed_validation": True,
                "failure_class": "validation_syntax_error",
                "traceback_product_failure": True,
            },
        )
    proxy._run_worker = failing_run_worker

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.",
        summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_real_fail", req)

    # ── Item 1 stays non-ok; item 2 never starts ───────────────────────────
    assert result.ok is not True, (
        "Validation failure must not be normalized to ok"
    )
    assert result.extras.get("work_artifact_item_completion_normalized") is not True, (
        "Normalized flag must not be set for real failures"
    )

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_real_fail")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status != WorkItemStatus.done, (
        "Item with real validation failure must not become done"
    )


def test_artifact_normalizer_behavioral_validation_skipped(tmp_path):
    """Test C: Behavioral validation skipped for artifact items.

    A WorkArtifact GUI/visual item with file writes and passing compileall
    (no declared UI probe) becomes done with caveat.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    # Item returns a result shaped like behavioral-skip + passing compileall
    def behavioral_skip_then_ok(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        return WorkerDispatchResult(
            ok=False,
            summary="Item completed. Behavioral validation skipped (caveat only).",
            recoverable=True,
            phase_boundary=True,
            needs_followup=True,
            status="completed_with_caveats",
            modified_files=list(worker_req.files),
            validation="python -m compileall src/model.py passed.",
            extras={
                "recoverable": True,
                "phase_boundary": True,
                "needs_followup": True,
                "suggested_next_tool": "dispatch_to_worker",
                "failure_class": "behavioral_validation_skipped",
            },
        )
    proxy._run_worker = behavioral_skip_then_ok

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.",
        summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_behavioral", req)

    # ── Artifact item completes through normalizer ─────────────────────────
    assert result.ok is True, (
        f"Artifact item with behavioral skip must complete, got ok={result.ok}"
    )
    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_behavioral")
    assert artifact is not None
    assert artifact.work_items[0].status == WorkItemStatus.done, (
        "Item 1 must be done even with behavioral validation skipped"
    )


def test_artifact_normalizer_receipt_corpse_prevention():
    """Test D: Receipt corpse prevention.

    A recoverable continuation result with successful item evidence
    (modified files + validation) must NOT produce ``receipt.status="continuing"``
    when routed through the artifact item normalizer.  It must produce
    ``receipt.status="ok"``.
    """
    from aura.bridge.dispatch import _DispatchProxy
    from aura.work_artifact.receipt import worker_result_to_receipt

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
    )

    # Build the continuing-but-successful result
    item_req = WorkerDispatchRequest(
        goal="Test",
        files=["src/model.py"],
        spec="Do the thing",
        acceptance="Works",
        summary="Test",
        artifact_id="art-1",
        artifact_item_id="item-1",
    )
    item_result = _continuing_with_evidence_result(
        modified=["src/model.py"],
    )

    # Without normalizer: receipt is "continuing" — the corpse state.
    raw_receipt = worker_result_to_receipt(item_result)
    assert raw_receipt.status == "continuing", (
        f"Expected continuing without normalizer, got {raw_receipt.status}"
    )

    # With normalizer: receipt is "ok"
    from aura.work_artifact.model import WorkArtifactItem
    dummy_item = WorkArtifactItem(
        id="item-1", title="Test", intent="Test",
        target_files=["src/model.py"], acceptance="Works",
    )
    normalized = proxy._normalize_artifact_item_completion(
        item_req, item_result, dummy_item,
    )
    ok_receipt = worker_result_to_receipt(normalized)
    assert ok_receipt.status == "ok", (
        f"Expected ok after normalizer, got {ok_receipt.status}"
    )


def test_artifact_normalizer_full_loop_both_items(tmp_path):
    """Test E: Full internal artifact loop.

    Item 1 succeeds through the normalized path (continuing-with-evidence).
    Item 2 starts automatically in the same approved artifact loop.
    Aggregate finishes ok when both items complete.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def continuing_then_ok(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            return _continuing_with_evidence_result(
                summary="Item 1 done — continuing signal suppressed by normalizer.",
                modified=list(worker_req.files),
            )
        return _ok_result(
            summary="Item 2 complete.",
            modified=list(worker_req.files),
        )
    proxy._run_worker = continuing_then_ok

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.",
        summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_full_loop", req)

    # ── Aggregate result: ok, both items complete ──────────────────────────
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    assert result.extras["completed_items"] == ["item-1", "item-2"]
    assert len(captured) == 2, (
        f"Expected 2 dispatches (item-1 and item-2), got {len(captured)}"
    )

    # ── No SpecCard or user approval occurred between items ─────────────────
    # Artifact is fully complete: both items done.
    ctrl = proxy.artifact_controller()
    assert ctrl.all_required_items_done("call_full_loop"), (
        "All items must be done after full loop"
    )

    # ── Item 1 has "completed" status (not "continuing") ───────────────────
    artifact = ctrl.get_artifact("call_full_loop")
    assert artifact is not None
    assert artifact.work_items[0].receipt is not None
    assert artifact.work_items[0].receipt.status == "ok"
    assert artifact.work_items[1].receipt is not None
    assert artifact.work_items[1].receipt.status == "ok"

    # ── Projection shows both done ─────────────────────────────────────────
    proj = WorkArtifactProjection.from_artifact(artifact)
    assert proj.completed_count == 2
    assert proj.pending_count == 0
    assert proj.is_complete is True


# ═══════════════════════════════════════════════════════════════════════════════
# O. Focused regression tests for item completion normalizer
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalizer_declared_validation_skipped_not_normalized(tmp_path):
    """Test 1: Declared validation skipped must not normalize.

    Two-item WorkArtifact.  Item 1 declares a validation command but the
    Worker result shows validation did not run (validation_results empty,
    validation_not_run=True).  The normalizer must NOT convert this to ok.

    Expected: item 1 stays non-done, receipt not ok, item 2 never starts,
    normalizer flag absent.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    # Item 1 always fails with validation_not_run; item 2 never reached.
    def failing_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        return WorkerDispatchResult(
            ok=False,
            summary="Validation did not run — continuing.",
            recoverable=True,
            needs_followup=True,
            phase_boundary=True,
            modified_files=list(worker_req.files),
            extras={
                "validation_results": [],
                "validation_not_run": True,
                "failure_class": "validation_not_run",
            },
        )
    proxy._run_worker = failing_run_worker

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    # Two-item payload; item 1 declares a validation command.
    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1", "title": "Add model",
                "intent": "Create the model",
                "target_files": ["src/model.py"],
                "acceptance": "Model works",
                "validation_commands": ["python -m compileall src/model.py"],
            },
            {
                "id": "item-2", "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.", summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_t1", req)

    # ── Item 1 must NOT be done ────────────────────────────────────────────
    assert result.ok is not True, "Job must not complete when validation skipped"
    assert result.extras.get("work_artifact_item_completion_normalized") is not True, (
        "Normalizer flag must not be set"
    )

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_t1")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status != WorkItemStatus.done, "Item 1 must not become done"

    # Receipt should not be "ok"
    if item1.receipt is not None:
        assert item1.receipt.status != "ok", "Item 1 receipt must not be ok"

    # ── Item 2 must NOT have started ──────────────────────────────────────
    item2 = artifact.work_items[1]
    assert item2.status == WorkItemStatus.pending, (
        f"Item 2 must remain pending, got {item2.status}"
    )
    assert not any(r.artifact_item_id == "item-2" for r in captured), (
        "Item 2 was dispatched despite item 1 not completing"
    )


def test_normalizer_declared_validation_passed_normalizes(tmp_path):
    """Test 2: Declared validation passed does normalize.

    Same shape as test 1 but the Worker result includes passing validation
    evidence (validation_results with passed classification).

    Expected: item 1 receipt.status == "ok", item 1 status == done,
    item 2 starts automatically.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def passing_then_ok(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            return WorkerDispatchResult(
                ok=False,
                summary="Item completed — acceptance unverified, continuing.",
                recoverable=True,
                phase_boundary=True,
                needs_followup=True,
                modified_files=list(worker_req.files),
                extras={
                    "validation_results": [
                        {
                            "command": "python -m compileall src/model.py",
                            "ok": True,
                            "exit_code": 0,
                            "validation_classification": "passed",
                            "counts_as_validation": True,
                            "counts_as_product_failure": False,
                        },
                    ],
                    "validation_not_run": False,
                    "failure_class": "validation_not_run",
                },
            )
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = passing_then_ok

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1", "title": "Add model",
                "intent": "Create the model",
                "target_files": ["src/model.py"],
                "acceptance": "Model works",
                "validation_commands": ["python -m compileall src/model.py"],
            },
            {
                "id": "item-2", "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.", summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_t2", req)

    # ── Both items completed ──────────────────────────────────────────────
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    assert result.extras["completed_items"] == ["item-1", "item-2"]

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_t2")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status == WorkItemStatus.done, (
        f"Item 1 expected done, got {item1.status}"
    )
    assert item1.receipt is not None
    assert item1.receipt.status == "ok", (
        f"Item 1 receipt expected 'ok', got '{item1.receipt.status}'"
    )

    # ── Item 2 started and completed ──────────────────────────────────────
    assert len(captured) == 2, (
        f"Expected 2 dispatches, got {len(captured)}"
    )


def test_normalizer_windows_path_mismatch(tmp_path):
    """Test 3: Windows path mismatch still normalizes.

    Target file is ``aura/gui/playground.py`` (forward slashes) but the
    Worker reports ``aura\\gui\\playground.py`` (backslashes).  The path
    normalizer must match them.

    Expected: item completes, receipt.status == "ok", stored modified_files
    preserve the original (backslash) path.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def windows_path_then_ok(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            # Return modified files with Windows backslashes
            return WorkerDispatchResult(
                ok=False,
                summary="Item completed — acceptance unverified, continuing.",
                recoverable=True,
                phase_boundary=True,
                needs_followup=True,
                modified_files=[f.replace("/", "\\") for f in worker_req.files],
                validation="python -m compileall passed.",
                extras={
                    "failure_class": "behavioral_validation_skipped",
                },
            )
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = windows_path_then_ok

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1", "title": "Add model",
                "intent": "Create the model",
                "target_files": ["aura/gui/playground.py"],
                "acceptance": "Model works",
            },
            {
                "id": "item-2", "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["aura/gui/playground.py"],
        spec="Implement.",
        acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_t3", req)

    # ── Both items completed ──────────────────────────────────────────────
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    assert result.extras["completed_items"] == ["item-1", "item-2"]

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_t3")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status == WorkItemStatus.done
    assert item1.receipt is not None
    assert item1.receipt.status == "ok"

    # ── Stored modified_files preserve the original backslash path ────────
    assert item1.receipt.modified_files == ["aura\\gui\\playground.py"], (
        f"Expected preserved backslash path, got {item1.receipt.modified_files}"
    )

    # ── Both items dispatched ─────────────────────────────────────────────
    assert len(captured) == 2, (
        f"Expected 2 dispatches, got {len(captured)}"
    )


def test_normalizer_explicit_behavioral_skipped_not_normalized(tmp_path):
    """Test 4: Explicit behavioral validation skipped must not normalize.

    WorkArtifact item declares behavioral/UI validation explicitly
    (pytest in validation_commands).  The Worker result shows the
    behavioral command was skipped.  The normalizer must NOT convert
    this to ok, even though file writes happened.

    Expected: ok remains false, item does not become done,
    flat dispatch behavior remains unchanged.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def behavioral_skip_fails(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        return WorkerDispatchResult(
            ok=False,
            summary="Required behavioral validation skipped: pytest.",
            recoverable=True,
            needs_followup=True,
            phase_boundary=True,
            modified_files=list(worker_req.files),
            extras={
                "required_behavioral_validation": {
                    "passed": [],
                    "skipped": ["pytest"],
                    "could_not_run": [],
                    "failed": [],
                },
                "failure_class": "required_behavioral_validation_skipped",
            },
        )
    proxy._run_worker = behavioral_skip_fails

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    # Item 1 declares behavioral validation via pytest.
    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1", "title": "Add model",
                "intent": "Create the model",
                "target_files": ["src/model.py"],
                "acceptance": "Model works",
                "validation_commands": ["pytest tests/test_model.py"],
            },
            {
                "id": "item-2", "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py"],
        spec="Implement.",
        acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_t4", req)

    # ── Job must NOT complete ─────────────────────────────────────────────
    assert result.ok is not True, "Must not normalize with explicit behavioral skip"
    assert result.extras.get("work_artifact_item_completion_normalized") is not True

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_t4")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status != WorkItemStatus.done, (
        "Item with explicit behavioral skip must not become done"
    )

    # ── Flat dispatch consistency: behavioral skip still non-ok ────────────
    # (Flat dispatch is tested in test_worker_behavioral_validation.py)


def test_normalizer_implicit_behavioral_caveat_completes(tmp_path):
    """Test 5: Implicit behavioral caveat may complete.

    WorkArtifact GUI/visual item with:
    - file write happened
    - compileall passed (non-behavioral command)
    - no declared UI probe / no explicit behavioral command
    - behavioral skip caveat exists in failure_class

    Expected: item completes with caveat, receipt.status == "ok",
    item status == done, item 2 starts automatically.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def behavioral_caveat_then_ok(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            return WorkerDispatchResult(
                ok=False,
                summary="Item completed. Behavioral validation skipped (caveat only).",
                recoverable=True,
                phase_boundary=True,
                needs_followup=True,
                modified_files=list(worker_req.files),
                validation="python -m compileall src/model.py passed.",
                extras={
                    "validation_results": [
                        {
                            "command": "python -m compileall src/model.py",
                            "ok": True,
                            "exit_code": 0,
                            "validation_classification": "passed",
                            "counts_as_validation": True,
                            "counts_as_product_failure": False,
                        },
                    ],
                    "failure_class": "behavioral_validation_skipped",
                },
            )
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = behavioral_caveat_then_ok

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    # Only non-behavioral validation (compileall), no UI probe.
    payload = {
        "goal": "Add feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1", "title": "Add model",
                "intent": "Create the model",
                "target_files": ["src/model.py"],
                "acceptance": "Model works",
                "validation_commands": ["python -m compileall src/model.py"],
            },
            {
                "id": "item-2", "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py"],
        spec="Implement.",
        acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_t5", req)

    # ── Both items completed ──────────────────────────────────────────────
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    assert result.extras["completed_items"] == ["item-1", "item-2"]

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_t5")
    assert artifact is not None

    # Item 1 done with caveat
    item1 = artifact.work_items[0]
    assert item1.status == WorkItemStatus.done, (
        f"Item 1 expected done, got {item1.status}"
    )
    assert item1.receipt is not None
    assert item1.receipt.status == "ok", (
        f"Item 1 receipt expected 'ok', got '{item1.receipt.status}'"
    )

    # Item 2 also done
    item2 = artifact.work_items[1]
    assert item2.status == WorkItemStatus.done

    # ── Projection shows all done ─────────────────────────────────────────
    proj = WorkArtifactProjection.from_artifact(artifact)
    assert proj.completed_count == 2
    assert proj.pending_count == 0
    assert proj.is_complete is True

    # ── Both items dispatched ─────────────────────────────────────────────
    assert len(captured) == 2, (
        f"Expected 2 dispatches, got {len(captured)}"
    )


def test_normalizer_real_validation_failure_not_normalized(tmp_path):
    """Test 6: Real validation failure never normalizes.

    Simulate compileall failure / syntax error / traceback product failure.

    Expected: item does not become done, normalizer flag absent,
    retry/pause behavior remains.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def failing_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        # Simulate a compileall failure with traceback
        return WorkerDispatchResult(
            ok=False,
            summary="Item failed: validation error — syntax error in src/model.py",
            status="validation_failed",
            modified_files=list(worker_req.files),
            validation="python -m compileall src/model.py failed with traceback.",
            extras={
                "failed_validation": True,
                "failure_class": "validation_syntax_error",
                "traceback_product_failure": True,
                "validation_results": [],
            },
        )
    proxy._run_worker = failing_run_worker

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.",
        summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_t6", req)

    # ── Item 1 stays non-ok; item 2 never starts ──────────────────────────
    assert result.ok is not True, (
        "Validation failure must not be normalized to ok"
    )
    assert result.extras.get("work_artifact_item_completion_normalized") is not True, (
        "Normalized flag must not be set for real failures"
    )

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_t6")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status != WorkItemStatus.done, (
        "Item with real validation failure must not become done"
    )


def test_normalizer_mismatch_not_normalized(tmp_path):
    """Test A: Mismatch not normalized.

    A continuing-with-evidence result that also carries a WorkerMismatch.
    The mismatch guard must fire before any evidence checks, preventing
    normalisation.

    Tests two paths:
    a) Direct normalizer call — mismatch object survives unchanged and
       ``worker_result_to_receipt`` produces ``status="mismatch"``.
    b) Full dispatch loop — mismatch prevents normalization, item 1 stays
       non-done (retried to cap), item 2 never starts.
    """
    from aura.bridge.dispatch import _DispatchProxy
    from aura.work_artifact.receipt import worker_result_to_receipt
    from aura.work_artifact.model import WorkArtifactItem

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    # ── Path a: Direct normalizer call ──────────────────────────────────
    item_req = WorkerDispatchRequest(
        goal="Test",
        files=["src/model.py"],
        spec="Do the thing",
        acceptance="Works",
        summary="Test",
        artifact_id="art-1",
        artifact_item_id="item-1",
    )
    mismatch_result = WorkerDispatchResult(
        ok=False,
        summary="Item completed — mismatch detected.",
        recoverable=True,
        phase_boundary=True,
        needs_followup=True,
        status="completed_with_caveats",
        modified_files=["src/model.py"],
        validation="python -m compileall src/model.py passed.",
        extras={
            "failure_class": "behavioral_validation_skipped",
        },
        mismatch=WorkerMismatch(
            kind="conflicting_spec",
            file_paths=["src/model.py"],
            requested="Add model class",
            observed="Model class already exists",
            worker_recommendation="Update existing model",
            question_for_planner="Should I update the existing model?",
        ),
    )

    dummy_item = WorkArtifactItem(
        id="item-1", title="Test", intent="Test",
        target_files=["src/model.py"], acceptance="Works",
    )
    normalized = proxy._normalize_artifact_item_completion(
        item_req, mismatch_result, dummy_item,
    )

    # Normalizer must return the result unchanged (not normalized)
    assert normalized is mismatch_result, "Mismatch result must pass through unchanged"
    assert normalized.mismatch is not None, "Mismatch must survive normalizer"
    assert normalized.mismatch.kind == "conflicting_spec"
    assert normalized.ok is False, "Mismatch result must not become ok"

    # When routed through receipt, status must be "mismatch"
    receipt = worker_result_to_receipt(normalized)
    assert receipt.status == "mismatch", (
        f"Expected receipt status 'mismatch', got '{receipt.status}'"
    )
    assert receipt.mismatch is not None
    assert receipt.mismatch.get("kind") == "conflicting_spec"

    # ── Path b: Full dispatch loop ──────────────────────────────────────
    captured: list[WorkerDispatchRequest] = []

    def mismatch_then_fail(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            return WorkerDispatchResult(
                ok=False,
                summary="Item completed — mismatch detected.",
                recoverable=True,
                phase_boundary=True,
                needs_followup=True,
                status="completed_with_caveats",
                modified_files=list(worker_req.files),
                validation="python -m compileall src/model.py passed.",
                extras={
                    "failure_class": "behavioral_validation_skipped",
                },
                mismatch=WorkerMismatch(
                    kind="conflicting_spec",
                    file_paths=["src/model.py"],
                    requested="Add model class",
                    observed="Model class already exists",
                    worker_recommendation="Update existing model",
                    question_for_planner="Should I update the existing model?",
                ),
            )
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = mismatch_then_fail

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_two_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.", summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_mismatch", req)

    # Job did NOT complete — normalization was prevented on every attempt
    assert result.ok is not True, "Mismatch must not normalise to ok"
    assert result.extras.get("work_artifact_item_completion_normalized") is not True

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_mismatch")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status != WorkItemStatus.done, "Item with mismatch must not become done"

    # The final receipt came from the retry-cap pause (no mismatch carried),
    # NOT from any single attempt's mismatch result — that's correct.
    # The important guard is that normalization never fired.

    # ── Item 2 must NOT have started ──
    assert not any(r.artifact_item_id == "item-2" for r in captured), (
        "Item 2 dispatched despite mismatch on item 1"
    )


def test_normalizer_prose_only_validation_evidence_not_normalized(tmp_path):
    """Test B: Prose-only validation evidence not normalized.

    Item 1 declares a validation command.  The Worker result carries
    ``validation="1 failed, 0 passed"`` (prose text containing substring
    "passed") but empty ``validation_results`` and ``terminal_results``.
    No ``failed_validation`` or ``validation_not_run`` signals.

    Without the prose-marker deletion in ``_artifact_item_validation_passed``,
    the substring "passed" would falsely match.  After deletion, the normaliser
    must not mark this item done.

    Expected: not normalised, item does not become done, item 2 does not start.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def prose_only_fails(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            return WorkerDispatchResult(
                ok=False,
                summary="Item completed — acceptance unverified, continuing.",
                recoverable=True,
                phase_boundary=True,
                needs_followup=True,
                modified_files=list(worker_req.files),
                validation="1 failed, 0 passed",
                extras={
                    "validation_results": [],
                    "terminal_results": [],
                },
            )
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = prose_only_fails

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1", "title": "Add model",
                "intent": "Create the model",
                "target_files": ["src/model.py"],
                "acceptance": "Model works",
                "validation_commands": ["python -m compileall src/model.py"],
            },
            {
                "id": "item-2", "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py", "src/b.py"],
        spec="Implement the full feature.",
        acceptance="Everything works.", summary="Full feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_prose", req)

    # ── Prose-only must NOT normalise ──
    assert result.ok is not True, "Prose-only validation must not normalise to ok"
    assert result.extras.get("work_artifact_item_completion_normalized") is not True

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_prose")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status != WorkItemStatus.done, (
        "Item with prose-only validation must not become done"
    )

    # ── Item 2 must NOT have started ──
    assert not any(r.artifact_item_id == "item-2" for r in captured), (
        "Item 2 dispatched despite item 1 not completing"
    )


def test_normalizer_absolute_modified_path_normalizes(tmp_path):
    """Test C: Absolute modified path does normalise.

    WorkArtifact item target files are relative
    (``aura/gui/cards/_helpers.py``) but the Worker reports absolute
    Windows paths (``C:\\Projects\\...\\aura\\gui\\cards\\_helpers.py``).
    The segment-suffix fallback in ``_artifact_item_paths_overlap`` must
    match them.

    Includes structured passing validation evidence so the normaliser
    reaches the path-overlap check.

    Expected: normalised to ok, receipt status "ok", item done,
    stored modified_files preserve the original absolute path,
    item 2 starts automatically.
    """
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchRequest] = []

    def absolute_path_then_ok(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_req)
        if worker_req.artifact_item_id == "item-1":
            return WorkerDispatchResult(
                ok=False,
                summary="Item completed — acceptance unverified, continuing.",
                recoverable=True,
                phase_boundary=True,
                needs_followup=True,
                status="completed_with_caveats",
                modified_files=[
                    f"C:\\Projects\\Aura-Harness2\\{f.replace('/', '\\')}"
                    for f in worker_req.files
                ],
                extras={
                    "validation_results": [
                        {
                            "command": "python -m compileall aura/gui/cards/_helpers.py",
                            "ok": True,
                            "exit_code": 0,
                            "validation_classification": "passed",
                            "counts_as_validation": True,
                            "counts_as_product_failure": False,
                        },
                    ],
                    "failure_class": "behavioral_validation_skipped",
                },
            )
        return _ok_result(modified=list(worker_req.files))
    proxy._run_worker = absolute_path_then_ok

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1", "title": "Add helpers",
                "intent": "Create the helpers module",
                "target_files": ["aura/gui/cards/_helpers.py"],
                "acceptance": "Helpers work",
            },
            {
                "id": "item-2", "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["aura/gui/cards/_helpers.py"],
        spec="Implement.",
        acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_abs_path", req)

    # ── Both items completed ──
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    assert result.extras["completed_items"] == ["item-1", "item-2"]

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_abs_path")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status == WorkItemStatus.done, (
        f"Item 1 expected done, got {item1.status}"
    )
    assert item1.receipt is not None
    assert item1.receipt.status == "ok", (
        f"Item 1 receipt expected 'ok', got '{item1.receipt.status}'"
    )

    # ── Normaliser flag must be set on per-item receipt metadata ──
    # (The flag lives on the per-item WorkerDispatchResult extras, not on
    # the job-level aggregate extras.)
    assert item1.receipt.metadata.get("work_artifact_item_completion_normalized") is True, (
        "Normaliser flag must be set on per-item receipt metadata"
    )

    # ── Stored modified_files preserve the original absolute path ──
    expected_abs = "C:\\Projects\\Aura-Harness2\\aura\\gui\\cards\\_helpers.py"
    assert item1.receipt.modified_files == [expected_abs], (
        f"Expected preserved absolute path, got {item1.receipt.modified_files}"
    )

    # ── Both items dispatched ──
    assert len(captured) == 2, (
        f"Expected 2 dispatches, got {len(captured)}"
    )
