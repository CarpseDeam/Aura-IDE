"""Tests for WorkArtifact dispatch integration.

Verifies the new contract:
- ToolRunner preserves the full approved job envelope.
- DispatchProxy runs all items internally under one approval.
- Recoverable item failures retry on the same item.
- Projection and card rendering report item-level truth.
- Worker prompt uses artifact-aware wording.
"""

from typing import Any

from aura.bridge.worker_report import _format_spec_as_user_message
from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.history import History
from aura.conversation.tool_runner import ToolRunner
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.model import (
    ValidationCommandSpec,
    WorkArtifact,
    WorkArtifactItem,
    WorkArtifactReceipt,
    WorkItemStatus,
)
from aura.work_artifact.projection import WorkArtifactProjection
from aura.work_artifact.receipt import worker_result_to_receipt

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


def test_controller_creates_one_item_artifact_from_flat_request():
    """Controller normalises a flat request into a one-item WorkArtifact.

    Every approved dispatch that carries no explicit ``work_artifact_payload``
    becomes a one-item artifact with fields mapped from the top-level request.
    """
    ctrl = WorkArtifactController()
    req = WorkerDispatchRequest(
        goal="Fix the parsing bug",
        files=["bug.py"],
        spec="Fix the parsing bug in bug.py",
        acceptance="Bug is fixed",
        summary="Bug fix in parsing module",
        validation_commands=[
            ValidationCommandSpec(command="python -m compileall bug.py"),
        ],
        non_goals=["No new dependencies"],
    )

    artifact = ctrl.create_artifact_from_request("call_flat", req)

    # One item with the correct identity
    assert artifact.artifact_id == "call_flat"
    assert len(artifact.work_items) == 1
    item = artifact.work_items[0]
    assert item.id == "item-1"
    assert item.title == "Bug fix in parsing module"  # from summary
    assert item.intent == "Fix the parsing bug"  # from goal
    assert item.target_files == ["bug.py"]  # from files
    assert item.acceptance == "Bug is fixed"  # from acceptance

    # Validation commands copied from top-level
    assert len(item.validation_commands) == 1
    assert item.validation_commands[0].command == "python -m compileall bug.py"

    # Artifact-level fields from the flat request
    assert artifact.goal == "Fix the parsing bug"
    assert artifact.constraints == ["No new dependencies"]  # from non_goals
    assert artifact.allowed_files == ["bug.py"]  # from files


def test_controller_one_item_artifact_goal_fallback():
    """One-item artifact uses goal when summary is empty."""
    ctrl = WorkArtifactController()
    req = WorkerDispatchRequest(
        goal="Fix the parsing bug",
        files=["bug.py"],
        spec="Fix it",
        acceptance="Fixed",
        summary="",
    )
    artifact = ctrl.create_artifact_from_request("call_fb", req)
    assert artifact.work_items[0].title == "Fix the parsing bug"


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

    # Receipt does NOT change status — must mark done explicitly.
    ctrl.mark_item_done("call_123", "item-1")
    ctrl.attach_receipt("call_123", _ok_result(modified=["src/model.py"]))
    pending2 = ctrl.pending_items("call_123")
    assert len(pending2) == 1  # item-1 done, item-2 still pending
    assert pending2[0].id == "item-2"

    # Mark item-2 done explicitly.
    ctrl.mark_item_active("call_123", "item-2")
    ctrl.mark_item_done("call_123", "item-2")
    ctrl.attach_receipt("call_123", _ok_result(modified=["src/view.py"]), item_id="item-2")
    assert ctrl.pending_items("call_123") == []


def test_controller_all_required_items_done():
    """all_required_items_done returns True only when all items are done."""
    ctrl = WorkArtifactController()
    ctrl.create_artifact_from_payload("call_123", _make_two_item_payload())

    assert not ctrl.all_required_items_done("call_123")

    ctrl.mark_item_active("call_123", "item-1")
    ctrl.mark_item_done("call_123", "item-1")
    ctrl.attach_receipt("call_123", _ok_result())
    assert not ctrl.all_required_items_done("call_123")

    ctrl.mark_item_active("call_123", "item-2")
    ctrl.mark_item_done("call_123", "item-2")
    ctrl.attach_receipt("call_123", _ok_result(), item_id="item-2")
    assert ctrl.all_required_items_done("call_123")


def test_controller_attach_receipt_with_explicit_item_id():
    """attach_receipt with item_id attaches to the correct item, not current_item.
    Receipts are records only — status must be set via mark_item_done."""
    ctrl = WorkArtifactController()
    ctrl.create_artifact_from_payload("call_123", _make_two_item_payload())

    # Attach to item-2 explicitly while item-1 is current
    ctrl.attach_receipt("call_123", _ok_result(modified=["src/view.py"]), item_id="item-2")
    # Receipt does NOT change status — must mark done explicitly.
    ctrl.mark_item_done("call_123", "item-2")
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
    ctrl = WorkArtifactController()
    payload = _make_two_item_payload()
    # Add an empty validation command to item-1 (simulating a declared but
    # empty acceptance validation command).
    payload["items"][0]["validation_commands"] = [
        ValidationCommandSpec(command="").to_dict(),
    ]
    ctrl.create_artifact_from_payload("call_cont", payload)

    # Run item-1: mark active, mark done, attach receipt as audit record.
    ctrl.mark_item_active("call_cont", "item-1")
    ctrl.mark_item_done("call_cont", "item-1")
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

    result = proxy._aggregate_artifact_results("call_1", approved_req, item_results, 2)

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

    result = proxy._aggregate_artifact_results("call_1", approved_req, item_results, 2)

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
        "call_1", approved_req, item_results, 3,
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


def test_request_dispatch_flat_dispatch_creates_one_item_artifact(tmp_path):
    """Flat dispatch (no work_artifact_payload) creates a one-item WorkArtifact
    and routes through WorkArtifactRunner.

    Regression: flat dispatch must no longer bypass the artifact system.
    The controller artifact exists, projection emits, and the item runs
    as a bounded artifact item request.
    """
    from aura.bridge.dispatch import _DispatchProxy

    captured: list[WorkerDispatchRequest] = []

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    _fake_call_count: list[int] = [0]
    _FAKE_MAX = 20

    def capturing_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        _fake_call_count[0] += 1
        if _fake_call_count[0] > _FAKE_MAX:
            raise RuntimeError(f"Fake worker called {_fake_call_count[0]} times — possible infinite loop")
        captured.append(worker_req)
        vc = worker_req.validation_commands or []
        evidence = []
        for v in vc:
            cmd = v.command if hasattr(v, 'command') else str(v)
            if cmd.strip():
                evidence.append({
                    "command": cmd, "ok": True, "exit_code": 0,
                    "validation_classification": "passed",
                    "counts_as_validation": True,
                    "counts_as_product_failure": False,
                })
        return WorkerDispatchResult(
            ok=True, summary="Done", modified_files=list(worker_req.files),
            extras={"validation_results": evidence} if evidence else {},
        )
    proxy._run_worker = capturing_run_worker

    # Bypass Qt signal: auto-resolve the pending dispatch immediately
    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    # Flat request — no work_artifact_payload
    req = WorkerDispatchRequest(
        goal="Fix the bug",
        files=["bug.py"],
        spec="Fix the parsing bug in bug.py",
        acceptance="Bug is fixed",
        summary="Bug fix in parsing module",
    )

    result = proxy.request_dispatch("call_flat_integration", req)

    # Job completed
    assert result.ok is True

    # Controller has a one-item artifact
    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_flat_integration")
    assert artifact is not None
    assert len(artifact.work_items) == 1
    assert artifact.work_items[0].id == "item-1"
    assert artifact.work_items[0].title == "Bug fix in parsing module"
    assert artifact.work_items[0].intent == "Fix the bug"
    assert artifact.work_items[0].target_files == ["bug.py"]

    # Exactly one bounded item request was dispatched
    assert len(captured) == 1
    r1 = captured[0]
    assert r1.artifact_id == "call_flat_integration"
    assert r1.artifact_item_id == "item-1"
    assert "WorkArtifact Item 1/1" in r1.spec
    assert "Complete only this item" in r1.spec

    # Artifact projection emits (artifact is present)
    assert ctrl.all_required_items_done("call_flat_integration")


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
    _fake_call_count: list[int] = [0]
    _FAKE_MAX = 20

    def capturing_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        _fake_call_count[0] += 1
        if _fake_call_count[0] > _FAKE_MAX:
            raise RuntimeError(f"Fake worker called {_fake_call_count[0]} times — possible infinite loop")
        captured.append(worker_req)
        # Return evidence matching any injected validation commands.
        vc = worker_req.validation_commands or []
        evidence = []
        for v in vc:
            cmd = v.command if hasattr(v, 'command') else str(v)
            if cmd.strip():
                evidence.append({
                    "command": cmd,
                    "ok": True,
                    "exit_code": 0,
                    "validation_classification": "passed",
                    "counts_as_validation": True,
                    "counts_as_product_failure": False,
                })
        return WorkerDispatchResult(
            ok=True,
            summary="Done",
            modified_files=list(worker_req.files),
            extras={"validation_results": evidence} if evidence else {},
        )
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
    artifact.mark_done("item-1")
    artifact.attach_receipt("item-1", WorkArtifactReceipt(status="ok"))
    proj2 = WorkArtifactProjection.from_artifact(artifact)
    assert proj2.completed_count == 1
    assert proj2.pending_count == 1
    assert proj2.is_complete is False  # not all done
    assert proj2.artifact_status == "active"  # some items still pending

    # Mark all done
    artifact.mark_active("item-2")
    artifact.mark_done("item-2")
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
    artifact.mark_done("item-1")
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
        "call_1", approved_req, item_results, 2,
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
        "call_1", approved_req, item_results, 2,
    )

    assert result.ok is False
    assert result.extras.get("recovery_exhausted") is not True
    assert "incomplete" in result.summary.lower()
    assert "✓ item-1" in result.summary
    assert "✗ item-2" in result.summary
    assert "Stalled" in result.summary


# ── I. Infrastructure failure detection ────────────────────────────────────────


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

    _fc: list[int] = [0]

    def capturing_run_worker(tid: str, req: WorkerDispatchRequest, pending) -> WorkerDispatchResult:
        _fc[0] += 1
        if _fc[0] > 20:
            raise RuntimeError(f"Fake worker called {_fc[0]} times")
        captured.append(req)
        vc = req.validation_commands or []
        evidence = []
        for v in vc:
            cmd = v.command if hasattr(v, 'command') else str(v)
            if cmd.strip():
                evidence.append({"command": cmd, "ok": True, "exit_code": 0,
                                 "validation_classification": "passed",
                                 "counts_as_validation": True,
                                 "counts_as_product_failure": False})
        return WorkerDispatchResult(
            ok=True, summary="Done", modified_files=list(req.files),
            extras={"validation_results": evidence} if evidence else {},
        )
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

    _fc3: list[int] = [0]

    def capturing_run_worker(tid, req, pending):
        _fc3[0] += 1
        if _fc3[0] > 20:
            raise RuntimeError(f"Fake worker called {_fc3[0]} times")
        captured.append(req)
        vc = req.validation_commands or []
        evidence = []
        for v in vc:
            cmd = v.command if hasattr(v, 'command') else str(v)
            if cmd.strip():
                evidence.append({"command": cmd, "ok": True, "exit_code": 0,
                                 "validation_classification": "passed",
                                 "counts_as_validation": True,
                                 "counts_as_product_failure": False})
        return WorkerDispatchResult(
            ok=True, summary="Done", modified_files=list(req.files),
            extras={"validation_results": evidence} if evidence else {},
        )
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


# ── M. (removed) Retry cap no longer pauses — ordinary repeated failures
#    retry the same item indefinitely.  See test_retry_threshold_cannot_pause_approved_artifact. ─────


# ── N. (removed) WorkArtifact item completion normalizer — the execution loop
#    now owns advancement via _decide_artifact_item_outcome, not via
#    _normalize_artifact_item_completion. ──────────────────────────────────────



# ═══════════════════════════════════════════════════════════════════════════════
# O. (removed) Old normalizer regression tests deleted.
#     Advancement is owned by _run_approved_artifact_job via
#     _decide_artifact_item_outcome.  See the new tests below.
# ═══════════════════════════════════════════════════════════════════════════════


def _harness_result() -> WorkerDispatchResult:
    """Non-ok Worker result with passing compileall evidence.

    Returns ok=False with structured compileall pass evidence, modified
    files matching the request, and stale recoverable/continuing signals
    that the execution loop must override.
    """
    return WorkerDispatchResult(
        ok=False,
        summary="Item completed — acceptance unverified, continuing.",
        recoverable=True,
        phase_boundary=True,
        needs_followup=True,
        modified_files=["src/model.py"],
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
            "validation_not_run": True,
            "failure_class": "validation_not_run",
            "recoverable": True,
            "suggested_next_tool": "dispatch_to_worker",
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P. New tests for single-owner advancement (replaces old normalizer approach)
# ═══════════════════════════════════════════════════════════════════════════════


def test_mark_active_updates_current_item():
    """mark_active sets current_item_id and item status consistently."""
    artifact = WorkArtifact(
        artifact_id="art-1",
        goal="Test",
        work_items=[
            WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            WorkArtifactItem(id="item-2", title="B", intent="I2", target_files=["b.py"], acceptance="A2"),
        ],
        current_item_id="item-1",
    )
    assert artifact.current_item_id == "item-1"

    artifact.mark_active("item-2")
    assert artifact.work_items[1].status == WorkItemStatus.active
    assert artifact.current_item_id == "item-2"

    proj = WorkArtifactProjection.from_artifact(artifact)
    assert proj.current_item_id == "item-2"


def test_artifact_done_does_not_require_raw_worker_ok(tmp_path):
    """Item becomes done despite raw ok=False when evidence satisfies done conditions."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    _fc4_count: list[int] = [0]
    step: list[int] = [0]

    def fake_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        _fc4_count[0] += 1
        if _fc4_count[0] > 20:
            raise RuntimeError(f"Fake worker called {_fc4_count[0]} times")
        step[0] += 1
        if step[0] == 1:
            # Item 1: raw ok=False but structured compileall pass evidence.
            return WorkerDispatchResult(
                ok=False,
                summary="Item 1 done.",
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
                    "validation_not_run": True,
                    "failure_class": "validation_not_run",
                    "recoverable": True,
                    "suggested_next_tool": "dispatch_to_worker",
                },
            )
        # Item 2: may have scoped py_compile command injected.
        vc = worker_req.validation_commands or []
        evidence = []
        for v in vc:
            cmd = v.command if hasattr(v, 'command') else str(v)
            if cmd.strip():
                evidence.append({"command": cmd, "ok": True, "exit_code": 0,
                                 "validation_classification": "passed",
                                 "counts_as_validation": True,
                                 "counts_as_product_failure": False})
        return WorkerDispatchResult(
            ok=True, summary="Item 2 done.",
            modified_files=list(worker_req.files),
            extras={"validation_results": evidence} if evidence else {},
        )
    proxy._run_worker = fake_run_worker

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
        spec="Implement.", acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_done_no_raw_ok", req)

    # ── Item 1 done despite raw ok False ────────────────────────────────────
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_done_no_raw_ok")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status == WorkItemStatus.done, f"Item 1 expected done, got {item1.status}"
    assert item1.receipt is not None
    assert item1.receipt.status == "ok", f"Item 1 receipt expected ok, got {item1.receipt.status}"

    # ── Item 2 started and completed ────────────────────────────────────────
    assert artifact.work_items[1].status == WorkItemStatus.done
    assert result.extras["completed_items"] == ["item-1", "item-2"]

    # ── Projection ──────────────────────────────────────────────────────────
    proj = WorkArtifactProjection.from_artifact(artifact)
    assert proj.completed_count == 2
    assert proj.pending_count == 0

    # ── No stale failure metadata from raw non-ok results ──────────────────
    assert "failed_item_id" not in result.extras, \
        f"Expected no failed_item_id in completed result, got {result.extras.get('failed_item_id')}"
    current_id = result.extras.get("current_item_id", "")
    assert current_id in ("", None) or current_id != "item-1", \
        f"current_item_id should not be 'item-1' for completed job, got {current_id!r}"



def test_retry_threshold_cannot_pause_approved_artifact(tmp_path):
    """Ordinary repeated failures do NOT pause the job at the old retry cap."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    _fc_retry: list[int] = [0]
    item2_count: list[int] = [0]

    def _evidence_fallback(req: WorkerDispatchRequest) -> WorkerDispatchResult:
        """Return passing evidence for the request's validation commands."""
        vc = req.validation_commands or []
        ev = []
        for v in vc:
            cmd = v.command if hasattr(v, 'command') else str(v)
            if cmd.strip():
                ev.append({"command": cmd, "ok": True, "exit_code": 0,
                           "validation_classification": "passed",
                           "counts_as_validation": True,
                           "counts_as_product_failure": False})
        return WorkerDispatchResult(
            ok=True, summary="Done",
            modified_files=list(req.files),
            extras={"validation_results": ev} if ev else {},
        )

    def fake_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        _fc_retry[0] += 1
        if _fc_retry[0] > 30:
            raise RuntimeError(f"Fake worker called {_fc_retry[0]} times")
        if worker_req.artifact_item_id == "item-1":
            return _evidence_fallback(worker_req)
        if worker_req.artifact_item_id == "item-2":
            item2_count[0] += 1
            if item2_count[0] <= 12:  # More than the old retry cap of 10
                return _recoverable_result()
            # Finally succeed with evidence.
            return _evidence_fallback(worker_req)
        # Item 3
        return _evidence_fallback(worker_req)
    proxy._run_worker = fake_run_worker

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_three_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py"],
        spec="Implement.", acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_no_cap_pause", req)

    # ── All three items completed ───────────────────────────────────────────
    assert result.ok is True, f"Expected ok=True, got {result.ok}"
    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_no_cap_pause")
    assert artifact is not None
    assert all(wi.status == WorkItemStatus.done for wi in artifact.work_items), (
        "All items must be done"
    )
    assert result.extras["completed_items"] == ["item-1", "item-2", "item-3"]

    # ── No retry-cap or unfinished extras ──────────────────────────────────
    extras = result.extras if isinstance(result.extras, dict) else {}
    assert extras.get("work_artifact_unfinished") is not True, (
        "Must not have work_artifact_unfinished"
    )
    assert extras.get("artifact_retry_cap_reached") is not True, (
        "Must not have artifact_retry_cap_reached"
    )


def test_infrastructure_pause_still_allowed(tmp_path):
    """Infrastructure failure still pauses the job — item 3 does not run."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    _fc_infra: list[int] = [0]

    def _evidence_infra(req: WorkerDispatchRequest) -> WorkerDispatchResult:
        vc = req.validation_commands or []
        ev = []
        for v in vc:
            cmd = v.command if hasattr(v, 'command') else str(v)
            if cmd.strip():
                ev.append({"command": cmd, "ok": True, "exit_code": 0,
                           "validation_classification": "passed",
                           "counts_as_validation": True,
                           "counts_as_product_failure": False})
        return WorkerDispatchResult(
            ok=True, summary="Done",
            modified_files=list(req.files),
            extras={"validation_results": ev} if ev else {},
        )

    def fake_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        _fc_infra[0] += 1
        if _fc_infra[0] > 10:
            raise RuntimeError(f"Fake worker called {_fc_infra[0]} times")
        if worker_req.artifact_item_id == "item-1":
            return _evidence_infra(worker_req)
        if worker_req.artifact_item_id == "item-2":
            # Infrastructure failure
            return WorkerDispatchResult(
                ok=False,
                summary="API error: 503 Service Unavailable",
                status="harness_error",
                extras={"api_errors": ["timeout"], "failure_class": "provider_unavailable"},
            )
        # Item 3 — should never be reached.
        return _evidence_infra(worker_req)
    proxy._run_worker = fake_run_worker

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    payload = _make_three_item_payload()
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py"],
        spec="Implement.", acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    result = proxy.request_dispatch("call_infra_pause", req)

    # ── Job pauses — item 3 does not run ───────────────────────────────────
    assert result.ok is False, "Infrastructure pause must not be ok"
    assert result.recoverable is True, "Must be recoverable"
    assert result.cancelled is False, "Must not be cancelled"
    extras = result.extras if isinstance(result.extras, dict) else {}
    assert extras.get("work_artifact_unfinished") is True, (
        "Must have work_artifact_unfinished"
    )
    assert "item-3" in extras.get("pending_item_ids", []), (
        "Item 3 must be in pending_item_ids"
    )

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_infra_pause")
    assert artifact is not None
    assert artifact.work_items[0].status == WorkItemStatus.done, "Item 1 must be done"
    assert artifact.work_items[1].status != WorkItemStatus.done, "Item 2 must not be done"
    assert artifact.work_items[2].status == WorkItemStatus.pending, "Item 3 must be pending"


def test_receipt_is_audit_not_advancement_authority(tmp_path):
    """Receipt conversion without override would not be ok; execution loop
    uses status_override='ok' only after deciding outcome is done."""
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=lambda mode: None,
        approval_proxy=None,
        workspace_root=tmp_path,
    )

    captured: list[WorkerDispatchResult] = []

    def fake_run_worker(
        tool_call_id: str, worker_req: WorkerDispatchRequest, pending,
    ) -> WorkerDispatchResult:
        captured.append(worker_result_to_receipt)
        # Return non-ok result with passing validation evidence.
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
                "failure_class": "behavioral_validation_skipped",
            },
        )
    proxy._run_worker = fake_run_worker

    original_register = proxy._pending_map.register

    def auto_register(tool_call_id, req):
        pending = original_register(tool_call_id, req)
        pending.edited_request = req
        pending.decision_event.set()
        return pending
    proxy._pending_map.register = auto_register

    # Single item — no validation commands declared.
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
            },
        ],
    }
    req = WorkerDispatchRequest(
        goal="Full feature", files=["src/a.py"],
        spec="Implement.", acceptance="Works.", summary="Feature",
        work_artifact_payload=payload,
    )

    # ── Raw Worker result ok is False ──────────────────────────────────────
    raw_result = fake_run_worker("call_receipt_test", req, None)
    assert raw_result.ok is False, "Raw Worker result must be non-ok"

    # ── Without override: receipt would not be "ok" ─────────────────────────
    raw_receipt = worker_result_to_receipt(raw_result)
    assert raw_receipt.status != "ok", (
        "Receipt without override must not be ok"
    )

    # ── With status_override: receipt is ok ─────────────────────────────────
    overridden_receipt = worker_result_to_receipt(raw_result, status_override="ok")
    assert overridden_receipt.status == "ok", (
        "Receipt with status_override='ok' must be ok"
    )

    # ── Full dispatch: execution loop decides done, attaches with override ──
    result = proxy.request_dispatch("call_receipt_test", req)
    assert result.ok is True, f"Expected ok=True, got {result.ok}"

    ctrl = proxy.artifact_controller()
    artifact = ctrl.get_artifact("call_receipt_test")
    assert artifact is not None
    item1 = artifact.work_items[0]
    assert item1.status == WorkItemStatus.done, (
        "Item must be done after execution loop decides done"
    )
    assert item1.receipt is not None
    assert item1.receipt.status == "ok", (
        "Item receipt must be ok"
    )

    # ── Raw Worker result ok remains False (not mutated) ───────────────────
    # The raw result that was pushed into the loop is still non-ok.
    # We verify by checking that the loop had to use status_override:"ok"
    # rather than relying on raw ok.
    assert raw_result.ok is False, "Raw Worker result ok must remain False"

