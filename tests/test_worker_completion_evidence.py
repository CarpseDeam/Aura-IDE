from __future__ import annotations

import json

import pytest

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_completion_result import prepare_worker_completion_result
from aura.bridge.worker_validation_results import _unrecovered_validation_failures
from aura.client import ToolResult
from aura.conversation import History, WorkerDispatchRequest, WorkerDispatchResult, normalize_worker_task
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.events import EventBus


class _ApprovalProxy:
    def consume_last_event(self):
        return None


def _completion_result(req: WorkerDispatchRequest, relay: WorkerEventRelay):
    history = History()
    history.append_assistant(
        {
            "role": "assistant",
            "content": "<status>completed</status>\n<validation>passed</validation>",
            "reasoning_content": None,
        }
    )
    task_spec = normalize_worker_task(req)
    completion = prepare_worker_completion_result(
        req=req,
        worker_history=history,
        task_spec=task_spec,
        relay=relay,
        context_gearbox={},
        internal_error=None,
        cleaned_scratch_files=[],
        final_validation_commands=[],
        workspace_root=None,
        preserve_scratch_records=False,
    )
    return completion.build_result(validation_selector=None).result


def _relay_with_events(*events: ToolResult) -> WorkerEventRelay:
    relay = WorkerEventRelay(_ApprovalProxy(), event_bus=EventBus())
    for event in events:
        relay.relay("worker-parent", event)
    return relay


def _validation_event() -> ToolResult:
    return ToolResult(
        tool_call_id="validation-1",
        name="run_terminal_command",
        ok=True,
        result=json.dumps(
            {
                "ok": True,
                "command": "python -m py_compile src/example.py",
                "exit_code": 0,
                "output": "",
            }
        ),
    )


@pytest.mark.parametrize("tool_name", ["write_file", "patch_file", "edit_file"])
def test_successful_write_edit_patch_result_with_touched_file_is_completion_evidence(
    tool_name: str,
) -> None:
    relay = _relay_with_events(
        ToolResult(
            tool_call_id="edit-1",
            name=tool_name,
            ok=True,
            result=json.dumps(
                {
                    "ok": True,
                    "path": "src/example.py",
                    "applied": True,
                    "applied_tool": tool_name,
                    "is_new_file": False,
                }
            ),
        ),
        _validation_event(),
    )
    req = WorkerDispatchRequest(
        goal="Implement the example change",
        files=["src/example.py"],
        spec="Update src/example.py.",
        acceptance="",
    )

    result = _completion_result(req, relay)

    assert result.ok is True
    assert result.status == WorkerOutcomeStatus.completed.value
    assert result.modified_files == ["src/example.py"]
    assert result.extras["writes"][0]["tool"] == tool_name
    assert result.extras.get("failure_class") != "harness_no_progress"


def _completion_result_with_workspace(
    req: WorkerDispatchRequest,
    relay: WorkerEventRelay,
    workspace_root,
) -> WorkerDispatchResult:
    history = History()
    history.append_assistant(
        {
            "role": "assistant",
            "content": "<status>completed</status>\n<validation>passed</validation>",
            "reasoning_content": None,
        }
    )
    task_spec = normalize_worker_task(req)
    completion = prepare_worker_completion_result(
        req=req,
        worker_history=history,
        task_spec=task_spec,
        relay=relay,
        context_gearbox={},
        internal_error=None,
        cleaned_scratch_files=[],
        final_validation_commands=[],
        workspace_root=workspace_root,
        preserve_scratch_records=False,
    )
    return completion.build_result(validation_selector=None).result


def test_audit_changed_files_not_called_during_completion_assembly(monkeypatch, tmp_path) -> None:
    """audit_changed_files must not be called during Worker completion assembly."""
    import aura.code_intel.audit

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("audit_changed_files was invoked but is no longer wired")
    monkeypatch.setattr(aura.code_intel.audit, "audit_changed_files", _should_not_be_called)

    relay = _relay_with_events(
        ToolResult(
            tool_call_id="edit-1",
            name="write_file",
            ok=True,
            result=json.dumps(
                {"ok": True, "path": "src/example.py", "applied": True, "is_new_file": False}
            ),
        ),
        _validation_event(),
    )
    # Touched files + workspace_root would have triggered the audit block.
    relay.touched_files.add("src/example.py")

    req = WorkerDispatchRequest(
        goal="Implement the example change",
        files=["src/example.py"],
        spec="Update src/example.py.",
        acceptance="",
    )

    result = _completion_result_with_workspace(req, relay, tmp_path)

    assert result.ok is True
    assert result.status == WorkerOutcomeStatus.completed.value


def test_validation_only_without_touched_files_is_not_implementation_work() -> None:
    implementation_req = WorkerDispatchRequest(
        goal="Implement the example change",
        files=["src/example.py"],
        spec="Update src/example.py.",
        acceptance="",
    )

    implementation_result = _completion_result(
        implementation_req,
        _relay_with_events(_validation_event()),
    )

    assert implementation_result.ok is False
    assert implementation_result.status == WorkerOutcomeStatus.harness_error.value
    assert implementation_result.modified_files == []
    assert implementation_result.extras.get("failure_class") == "harness_no_progress"

    validation_only_req = WorkerDispatchRequest(
        goal="Validation only: run the focused check",
        files=["src/example.py"],
        spec="Run validation only for src/example.py.",
        acceptance="",
    )

    validation_only_result = _completion_result(
        validation_only_req,
        _relay_with_events(_validation_event()),
    )

    assert validation_only_result.ok is True
    assert validation_only_result.status == WorkerOutcomeStatus.completed.value
    assert validation_only_result.modified_files == []


# ── Validation family recovery tests ────────────────────────────────────────


def test_unrecovered_ruff_check_recovered_by_later_focused_ruff() -> None:
    """Failed ruff check is NOT an unrecovered failure when a later focused ruff passes."""
    results = [
        {
            "ok": False,
            "command": "ruff check",
            "exit_code": 1,
            "counts_as_product_failure": True,
        },
        {
            "ok": True,
            "command": "python -m ruff check aura/gui/left_pane.py",
            "exit_code": 0,
            "counts_as_product_failure": False,
        },
    ]
    failures = _unrecovered_validation_failures(results)
    assert failures == []


def test_unrecovered_pytest_recovered_by_later_focused_pytest() -> None:
    """Failed pytest is NOT an unrecovered failure when a later focused pytest passes."""
    results = [
        {
            "ok": False,
            "command": "pytest",
            "exit_code": 1,
            "counts_as_product_failure": True,
        },
        {
            "ok": True,
            "command": "python -m pytest tests/test_left_pane.py -q",
            "exit_code": 0,
            "counts_as_product_failure": False,
        },
    ]
    failures = _unrecovered_validation_failures(results)
    assert failures == []


def test_unrecovered_ruff_not_recovered_by_unrelated_validation() -> None:
    """Failed ruff check remains unrecovered when no later same-family pass exists."""
    results = [
        {
            "ok": False,
            "command": "ruff check",
            "exit_code": 1,
            "counts_as_product_failure": True,
        },
        {
            "ok": True,
            "command": "python -m py_compile src/example.py",
            "exit_code": 0,
            "counts_as_product_failure": False,
        },
    ]
    failures = _unrecovered_validation_failures(results)
    assert len(failures) == 1
    assert failures[0]["command"] == "ruff check"


def test_unrecovered_aura_selfcheck_recovered_by_later_selfcheck() -> None:
    """Failed aura selfcheck is NOT an unrecovered failure when a later aura selfcheck passes."""
    results = [
        {
            "ok": False,
            "command": "python -m aura --selfcheck",
            "exit_code": 1,
            "counts_as_product_failure": True,
        },
        {
            "ok": True,
            "command": "python -m aura --selfcheck",
            "exit_code": 0,
            "counts_as_product_failure": False,
        },
    ]
    failures = _unrecovered_validation_failures(results)
    assert failures == []


def test_unrecovered_aura_selfcheck_not_recovered_by_unrelated_validation() -> None:
    """Failed aura selfcheck remains unrecovered when no later same-family pass exists."""
    results = [
        {
            "ok": False,
            "command": "python -m aura --selfcheck",
            "exit_code": 1,
            "counts_as_product_failure": True,
        },
        {
            "ok": True,
            "command": "python -m py_compile src/example.py",
            "exit_code": 0,
            "counts_as_product_failure": False,
        },
    ]
    failures = _unrecovered_validation_failures(results)
    assert len(failures) == 1
    assert failures[0]["command"] == "python -m aura --selfcheck"
