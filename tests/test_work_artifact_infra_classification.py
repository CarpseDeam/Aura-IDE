"""Focused regressions for WorkArtifact infrastructure classification.

These tests lock the item-advancement seam: ``status=harness_error`` alone
must not pause a WorkArtifact, while explicit infrastructure markers still do.
"""
from __future__ import annotations

import threading

from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerOutcomeStatus,
)
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.runner import WorkArtifactRunner
from aura.work_artifact.verification import classify_item_attempt


def _request() -> WorkerDispatchRequest:
    return WorkerDispatchRequest(
        goal="G",
        files=[],
        spec="S",
        acceptance="A",
        summary="S",
    )


def test_harness_error_with_empty_extras_classifies_done_not_pause():
    result = WorkerDispatchResult(
        ok=False,
        status=WorkerOutcomeStatus.harness_error.value,
        summary="Worker returned a hard non-infrastructure result.",
        extras={},
    )

    outcome = classify_item_attempt(_request(), result)

    assert outcome.value == "done"


def test_harness_error_with_worker_internal_error_classifies_pause():
    result = WorkerDispatchResult(
        ok=False,
        status=WorkerOutcomeStatus.harness_error.value,
        summary="Worker internal exception.",
        extras={"worker_internal_error": True},
    )

    outcome = classify_item_attempt(_request(), result)

    assert outcome.value == "pause"


def test_harness_error_with_internal_error_marker_classifies_pause():
    result = WorkerDispatchResult(
        ok=False,
        status=WorkerOutcomeStatus.harness_error.value,
        summary="Internal harness exception.",
        extras={"internal_error": "RuntimeError: boom"},
    )

    outcome = classify_item_attempt(_request(), result)

    assert outcome.value == "pause"


def test_api_errors_classifies_pause():
    result = WorkerDispatchResult(
        ok=False,
        status=WorkerOutcomeStatus.harness_error.value,
        summary="Provider unavailable.",
        extras={"api_errors": ["503 Service Unavailable"]},
    )

    outcome = classify_item_attempt(_request(), result)

    assert outcome.value == "pause"


def test_cancellation_classifies_cancelled():
    result = WorkerDispatchResult(
        ok=False,
        cancelled=True,
        status=WorkerOutcomeStatus.cancelled.value,
        summary="User cancelled.",
        extras={},
    )

    outcome = classify_item_attempt(_request(), result)

    assert outcome.value == "cancelled"


def test_runner_advances_past_harness_error_with_empty_extras():
    ctrl = WorkArtifactController()
    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1",
                "title": "Add model",
                "intent": "Create the model",
                "target_files": ["src/model.py"],
                "acceptance": "Model works",
            },
            {
                "id": "item-2",
                "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    ctrl.create_artifact_from_payload("call_advance_empty_extras", payload)

    approved_req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py"],
        spec="Implement.",
        acceptance="Works.",
        summary="Feature",
        work_artifact_payload=payload,
    )

    captured: list[WorkerDispatchRequest] = []

    def fake_run_worker(_tool_call_id: str, item_req: WorkerDispatchRequest) -> WorkerDispatchResult:
        captured.append(item_req)
        if item_req.artifact_item_id == "item-1":
            return WorkerDispatchResult(
                ok=False,
                status=WorkerOutcomeStatus.harness_error.value,
                summary="Non-infrastructure hard result.",
                modified_files=["src/model.py"],
                extras={},
            )
        return WorkerDispatchResult(
            ok=True,
            status=WorkerOutcomeStatus.completed.value,
            summary="Done.",
            modified_files=["src/view.py"],
            extras={},
        )

    runner = WorkArtifactRunner(
        controller=ctrl,
        run_worker=fake_run_worker,
        emit_projection=lambda _artifact_id: None,
    )

    result = runner.run(
        "call_advance_empty_extras",
        approved_req,
        threading.Event(),
    )

    assert [req.artifact_item_id for req in captured] == ["item-1", "item-2"]
    artifact = ctrl.get_artifact("call_advance_empty_extras")
    assert artifact is not None
    assert artifact.work_items[0].status.value == "done"
    assert artifact.work_items[1].status.value == "done"
    assert result.ok is True
    assert result.extras["completed_items"] == ["item-1", "item-2"]


def test_runner_preserves_cancellation_behavior():
    ctrl = WorkArtifactController()
    payload = {
        "goal": "Implement feature",
        "constraints": [],
        "allowed_files": ["src/"],
        "items": [
            {
                "id": "item-1",
                "title": "Add model",
                "intent": "Create the model",
                "target_files": ["src/model.py"],
                "acceptance": "Model works",
            },
            {
                "id": "item-2",
                "title": "Add view",
                "intent": "Create the view",
                "target_files": ["src/view.py"],
                "acceptance": "View works",
            },
        ],
    }
    ctrl.create_artifact_from_payload("call_cancel", payload)

    approved_req = WorkerDispatchRequest(
        goal="Full feature",
        files=["src/a.py"],
        spec="Implement.",
        acceptance="Works.",
        summary="Feature",
        work_artifact_payload=payload,
    )

    captured: list[WorkerDispatchRequest] = []

    def fake_run_worker(_tool_call_id: str, item_req: WorkerDispatchRequest) -> WorkerDispatchResult:
        captured.append(item_req)
        return WorkerDispatchResult(
            ok=False,
            cancelled=True,
            status=WorkerOutcomeStatus.cancelled.value,
            summary="User cancelled.",
            extras={},
        )

    runner = WorkArtifactRunner(
        controller=ctrl,
        run_worker=fake_run_worker,
        emit_projection=lambda _artifact_id: None,
    )

    result = runner.run("call_cancel", approved_req, threading.Event())

    assert [req.artifact_item_id for req in captured] == ["item-1"]
    artifact = ctrl.get_artifact("call_cancel")
    assert artifact is not None
    assert artifact.work_items[0].status.value != "done"
    assert artifact.work_items[1].status.value == "pending"
    assert result.cancelled is True
