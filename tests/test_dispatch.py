"""Tests for aura.conversation.dispatch — WorkerDispatchRequest/Result."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from aura.bridge.dispatch import (
    _applied_modified_files,
    _build_worker_summary,
    _build_worker_validation_selector_plan,
    _combine_validation_commands,
    _compute_outcome_status,
    _diagnostic_environment_caveats,
    _filter_scratch_validation_results,
    _filter_scratch_write_records,
    _final_report_claims_failure,
    _final_report_claims_validation,
    _format_spec_as_user_message,
    _is_validation_scratch_path,
    _normalize_worker_path,
    _parse_continuation_report,
    _parse_structured_worker_failure,
    _unrecovered_not_applied_writes,
    _unrecovered_validation_failures,
    _validation_results_for_task,
    _validation_selector_changed_files,
    _validation_selector_commands,
    _workspace_file_exists,
)
from aura.bridge.dispatch_session import DispatchSession
from aura.bridge.event_relay import _is_validation_terminal_record
from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerMismatch,
    WorkerOutcomeStatus,
    WorkerTaskSpec,
    infer_outcome_status,
    normalize_outcome_status,
    normalize_worker_task,
)
from aura.conversation.dispatch_plan import (
    WorkerStepSpec,
    plan_from_request,
    todo_tasks_from_plan,
    validate_dispatch_campaign,
)
from aura.conversation.history import History
from aura.conversation.task_shape import TaskShape, infer_task_shape
from aura.conversation.tools._schemas import DISPATCH_TOOL_DEF

# WorkerDispatchRequest


def _dispatch_schema_text() -> str:
    return json.dumps(DISPATCH_TOOL_DEF, sort_keys=True)


def test_dispatch_to_worker_schema_includes_optional_steps_campaign():
    properties = DISPATCH_TOOL_DEF["function"]["parameters"]["properties"]

    steps = properties["steps"]
    step_properties = steps["items"]["properties"]

    assert steps["type"] == "array"
    assert "ordered campaign" in steps["description"]
    assert "single-step dispatch path" in steps["description"]
    assert set(step_properties) == {
        "id",
        "title",
        "goal",
        "spec",
        "files",
        "acceptance",
        "validation_commands",
        "required_outputs",
        "non_goals",
        "expected_public_symbols",
        "expected_dataclass_fields",
        "forbidden_calls",
        "forbidden_public_methods",
    }
    assert "steps" not in DISPATCH_TOOL_DEF["function"]["parameters"]["required"]
    assert "required for non-trivial implementation work" in steps["description"]


def test_dispatch_to_worker_schema_does_not_force_first_slice_language():
    schema_text = _dispatch_schema_text().lower()

    assert "dispatch only the first" not in schema_text
    assert "first concrete slice" not in schema_text
    assert "one helper family, one new module" not in schema_text


def test_plan_from_request_flat_request_keeps_single_step_shape():
    req = WorkerDispatchRequest(
        goal="Fix widget",
        files=["a.py", "b.py"],
        target_regions=[{"path": "a.py", "symbol": "Widget", "start_line": 12}],
        spec="Make the widget deterministic.",
        acceptance="Run focused validation.",
        summary="Widget determinism",
        validation_commands=["python -m py_compile a.py b.py"],
        required_outputs=["updated widget"],
        non_goals=["no UI redesign"],
        expected_public_symbols=["Widget"],
        expected_dataclass_fields={"WidgetState": ["value"]},
        forbidden_public_methods=["Widget.reset_all"],
        forbidden_calls=["random.random"],
        risk_notes=["shared state"],
    )

    plan = plan_from_request(req)

    assert plan.to_dict() == {
        "overall_goal": "Fix widget",
        "visible_summary": "Widget determinism",
        "global_files": ["a.py", "b.py"],
        "global_non_goals": ["no UI redesign"],
        "steps": [
            {
                "id": "step-1",
                "title": "Widget determinism",
                "goal": "Fix widget",
                "spec": "Make the widget deterministic.",
                "files": ["a.py", "b.py"],
                "target_regions": [{"path": "a.py", "symbol": "Widget", "start_line": 12}],
                "acceptance": "Run focused validation.",
                "validation_commands": ["python -m py_compile a.py b.py"],
                "required_outputs": ["updated widget"],
                "non_goals": ["no UI redesign"],
                "expected_public_symbols": ["Widget"],
                "expected_dataclass_fields": {"WidgetState": ["value"]},
                "forbidden_calls": ["random.random"],
                "forbidden_public_methods": ["Widget.reset_all"],
                "risk_notes": ["shared state"],
                "validation_policy": {
                    "tier": "focused",
                    "commands": ["python -m py_compile a.py b.py"],
                    "required_proofs": ["Run focused validation."],
                    "run_full_campaign_validation": False,
                    "reason": "compatibility conversion from WorkerDispatchRequest",
                },
            }
        ],
    }


def test_plan_from_request_uses_explicit_steps_in_order():
    steps = [
        WorkerStepSpec(id="prep", title="Prepare", goal="Prepare data", files=["prep.py"]),
        WorkerStepSpec(id="wire", title="Wire", goal="Wire behavior", files=["wire.py"]),
        WorkerStepSpec(id="check", title="Check", goal="Validate behavior", files=["test_wire.py"]),
    ]
    req = WorkerDispatchRequest(
        goal="Complete campaign",
        files=["shared.py"],
        spec="Top-level fallback spec",
        acceptance="Top-level acceptance",
        summary="Visible campaign",
        non_goals=["no schema migration"],
        steps=steps,
    )

    plan = plan_from_request(req)

    assert plan.overall_goal == "Complete campaign"
    assert plan.visible_summary == "Visible campaign"
    assert plan.global_files == ["shared.py"]
    assert plan.global_non_goals == ["no schema migration"]
    assert [step.id for step in plan.steps] == ["prep", "wire", "check"]
    assert plan.steps == steps
    assert plan.steps[0] is steps[0]


def test_todo_tasks_from_plan_projects_campaign_steps_not_summary():
    steps = [
        WorkerStepSpec(id="schema", title="Tighten schema", goal="Update tool schema", files=["a.py"]),
        WorkerStepSpec(id="runner", title="Reject bad dispatches", goal="Add runtime guard", files=["b.py"]),
        WorkerStepSpec(id="todo", title="Project campaign TODOs", goal="Keep TODOs step-based", files=["c.py"]),
    ]
    plan = plan_from_request(
        WorkerDispatchRequest(
            goal="Fix campaign planning",
            files=["a.py", "b.py", "c.py"],
            spec="Top-level campaign spec",
            acceptance="Focused tests pass.",
            summary="One broad summary that must not become the only TODO",
            steps=steps,
        )
    )

    tasks = todo_tasks_from_plan(plan, active_step_id="runner", completed_step_ids={"schema"})

    assert [task["description"] for task in tasks] == [
        "Tighten schema",
        "Reject bad dispatches",
        "Project campaign TODOs",
    ]
    assert "One broad summary" not in {task["description"] for task in tasks}
    assert [task["status"] for task in tasks] == ["done", "active", "pending"]


def test_campaign_validation_rejects_broad_flat_dispatch():
    req = WorkerDispatchRequest(
        goal="Build behavioral verification rung",
        files=["a.py", "b.py", "c.py"],
        spec="Add a validation rung across the dispatch subsystem.",
        acceptance="Run compileall and focused dispatch tests.",
        summary="Build the behavioral verification rung",
    )

    result = validate_dispatch_campaign(req)

    assert result.ok is False
    assert result.requires_steps is True
    assert any("decomposed steps campaign" in error for error in result.errors)


def test_campaign_validation_rejects_one_giant_broad_step():
    req = WorkerDispatchRequest(
        goal="Build behavioral verification rung",
        files=["a.py", "b.py", "c.py"],
        spec="Add a validation rung across the dispatch subsystem.",
        acceptance="Run compileall and focused dispatch tests.",
        summary="Build behavioral verification rung",
        steps=[
            WorkerStepSpec(
                id="step-1",
                title="Build behavioral verification rung",
                goal="Build behavioral verification rung",
                spec="Add a validation rung across the dispatch subsystem.",
                files=["a.py", "b.py", "c.py"],
            )
        ],
    )

    result = validate_dispatch_campaign(req)

    assert result.ok is False
    assert any("single campaign step" in error for error in result.errors)
    assert any("specific title" in error for error in result.errors)


def test_campaign_validation_allows_tiny_flat_cleanup():
    req = WorkerDispatchRequest(
        goal="Fix README typo",
        files=["README.md"],
        spec="Correct the typo in the usage note.",
        acceptance="Verify the typo is corrected.",
        summary="README typo cleanup",
    )

    result = validate_dispatch_campaign(req)

    assert result.ok is True
    assert result.requires_steps is False


def test_campaign_validation_allows_tiny_one_step_cleanup():
    req = WorkerDispatchRequest(
        goal="Fix README typo",
        files=["README.md"],
        spec="Correct the typo in the usage note.",
        acceptance="Verify the typo is corrected.",
        summary="README typo cleanup",
        steps=[
            WorkerStepSpec(
                id="step-1",
                title="README typo cleanup",
                goal="Fix README typo",
                spec="Correct the typo in the usage note.",
                files=["README.md"],
            )
        ],
    )

    result = validate_dispatch_campaign(req)

    assert result.ok is True
    assert result.requires_steps is False


def test_worker_dispatch_request_from_dict_accepts_explicit_steps():
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "Complete campaign",
            "files": ["shared.py"],
            "spec": "",
            "acceptance": "",
            "summary": "Visible campaign",
            "steps": [
                {"id": "one", "title": "One", "goal": "First step", "files": ["one.py"]},
                {"id": "two", "title": "Two", "goal": "Second step", "files": ["two.py"]},
                {"id": "three", "title": "Three", "goal": "Third step", "files": ["three.py"]},
            ],
        }
    )

    plan = plan_from_request(req)

    assert [step.id for step in req.steps] == ["one", "two", "three"]
    assert [step.goal for step in plan.steps] == ["First step", "Second step", "Third step"]


def test_dispatch_session_marks_internal_campaign_continuation():
    req = WorkerDispatchRequest(
        goal="Complete campaign",
        files=["shared.py"],
        spec="",
        acceptance="",
        steps=[
            WorkerStepSpec(id="one", title="One", goal="First step", files=["one.py"]),
            WorkerStepSpec(id="two", title="Two", goal="Second step", files=["two.py"]),
        ],
    )
    plan = plan_from_request(req)

    def run_worker_step(_tool_call_id, _step_request, _pending):
        return WorkerDispatchResult(
            ok=False,
            summary="Needs another internal pass.",
            needs_followup=True,
            recoverable=True,
            status=WorkerOutcomeStatus.needs_followup.value,
            extras={"recoverable": True},
        )

    result = DispatchSession(
        tool_call_id="tc1",
        original_request=req,
        plan=plan,
        run_worker_step=run_worker_step,
        pending=SimpleNamespace(),
    ).run()

    assert result.extras["dispatch_session"] is True
    assert result.extras["internal_campaign_continuation"] is True
    assert result.extras["suppress_user_followup_card"] is True
    assert result.extras["user_visible_blocker"] is False


def test_dispatch_session_user_only_blocker_stays_visible():
    req = WorkerDispatchRequest(
        goal="Complete campaign",
        files=["shared.py"],
        spec="",
        acceptance="",
        steps=[
            WorkerStepSpec(id="one", title="One", goal="First step", files=["one.py"]),
            WorkerStepSpec(id="two", title="Two", goal="Second step", files=["two.py"]),
        ],
    )
    plan = plan_from_request(req)

    def run_worker_step(_tool_call_id, _step_request, _pending):
        return WorkerDispatchResult(
            ok=False,
            summary="User decision required.",
            needs_followup=True,
            recoverable=True,
            status=WorkerOutcomeStatus.needs_followup.value,
            extras={"user_only_blocker": True},
        )

    result = DispatchSession(
        tool_call_id="tc1",
        original_request=req,
        plan=plan,
        run_worker_step=run_worker_step,
        pending=SimpleNamespace(),
    ).run()

    assert result.extras["internal_campaign_continuation"] is False
    assert result.extras["suppress_user_followup_card"] is False
    assert result.extras["user_visible_blocker"] is True


def test_dispatch_session_internal_worker_exception_routes_to_campaign_recovery():
    req = WorkerDispatchRequest(
        goal="Complete campaign",
        files=["shared.py"],
        spec="",
        acceptance="",
        steps=[
            WorkerStepSpec(id="one", title="One", goal="First step", files=["one.py"]),
            WorkerStepSpec(id="two", title="Two", goal="Second step", files=["two.py"]),
        ],
    )
    plan = plan_from_request(req)

    def run_worker_step(_tool_call_id, _step_request, _pending):
        return WorkerDispatchResult(
            ok=False,
            summary="Harness error due to an internal Worker dispatch exception.",
            status=WorkerOutcomeStatus.harness_error.value,
            extras={
                "worker_internal_error": True,
                "internal_error": "RuntimeError: hidden",
            },
        )

    result = DispatchSession(
        tool_call_id="tc1",
        original_request=req,
        plan=plan,
        run_worker_step=run_worker_step,
        pending=SimpleNamespace(),
    ).run()

    assert result.ok is False
    assert result.needs_followup is True
    assert result.recoverable is True
    assert result.status == WorkerOutcomeStatus.needs_followup.value
    assert not result.summary.startswith("Harness error")
    assert result.extras["worker_internal_error"] is True
    assert result.extras["internal_error"] == "RuntimeError: hidden"
    assert result.extras["campaign_recovery_classification"] == "internal_recoverable_error"
    assert result.extras["internal_campaign_continuation"] is True
    assert result.extras["suppress_user_followup_card"] is True
    assert result.extras["user_visible_blocker"] is False
    assert result.extras["campaign_recovery_attempts"] == {"one": 1}
    assert result.extras["campaign_recovery_budget"] == 1
    assert result.extras["campaign_recovery_budget_exhausted"] is True


def test_dispatch_session_environment_blocker_stays_user_visible():
    req = WorkerDispatchRequest(
        goal="Complete campaign",
        files=["shared.py"],
        spec="",
        acceptance="",
        steps=[
            WorkerStepSpec(id="one", title="One", goal="First step", files=["one.py"]),
            WorkerStepSpec(id="two", title="Two", goal="Second step", files=["two.py"]),
        ],
    )
    plan = plan_from_request(req)

    def run_worker_step(_tool_call_id, _step_request, _pending):
        return WorkerDispatchResult(
            ok=False,
            summary="Project environment missing dependency 'pytest'.",
            needs_followup=True,
            recoverable=True,
            status=WorkerOutcomeStatus.needs_followup.value,
            extras={
                "environment_setup_blockers": [
                    {"failure_class": "project_environment_missing_dependency"}
                ],
            },
        )

    result = DispatchSession(
        tool_call_id="tc1",
        original_request=req,
        plan=plan,
        run_worker_step=run_worker_step,
        pending=SimpleNamespace(),
    ).run()

    assert result.extras["campaign_recovery_classification"] == "terminal_environment_blocker"
    assert result.extras["internal_campaign_continuation"] is False
    assert result.extras["suppress_user_followup_card"] is False
    assert result.extras["user_visible_blocker"] is True
    assert result.extras["terminal_environment_blocker"] is True


def test_dispatch_session_explicit_campaign_emits_one_started_and_finished():
    req = WorkerDispatchRequest(
        goal="Complete campaign",
        files=["shared.py"],
        spec="",
        acceptance="",
        steps=[
            WorkerStepSpec(
                id="one",
                title="One",
                goal="First step",
                spec="Edit one.py.",
                files=["one.py"],
            ),
            WorkerStepSpec(
                id="two",
                title="Two",
                goal="Second step",
                spec="Edit two.py.",
                files=["two.py"],
            ),
        ],
    )
    plan = plan_from_request(req)
    started = []
    finished = []
    run_goals = []

    def run_worker_step(_tool_call_id, step_request, _pending):
        run_goals.append(step_request.goal)
        return WorkerDispatchResult(ok=True, summary=f"done {step_request.goal}")

    result = DispatchSession(
        tool_call_id="tc1",
        original_request=req,
        plan=plan,
        run_worker_step=run_worker_step,
        pending=SimpleNamespace(),
        emit_worker_started=started.append,
        emit_worker_finished=lambda *args: finished.append(args),
    ).run()

    assert result.ok is True
    assert run_goals == ["First step", "Second step"]
    assert started == ["tc1"]
    assert len(finished) == 1
    assert finished[0][0] == "tc1"
    assert finished[0][1] is True


def test_canonical_dispatch_todos_suppress_worker_local_updates():
    """During canonical dispatch, worker-local TODO updates are absorbed,
    not forwarded to the GUI. Only canonical snapshots reach the GUI."""
    from unittest.mock import Mock

    from aura.bridge.dispatch import _DispatchProxy
    from aura.bridge.todo_controller import DispatchTodoController

    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=Mock(),
        workspace_root=None,
    )
    emitted = []
    proxy.workerTodoListUpdated.connect(lambda tool_call_id, tasks: emitted.append((tool_call_id, tasks)))

    # Initialize canonical state and emit the initial snapshot
    proxy._todo_controller.begin("tc1", [
        {"id": "step-1", "description": "Campaign step"},
    ])
    # Emit the initial canonical snapshot (simulating what DispatchSession does)
    proxy._emit_canonical_dispatch_todos("tc1", proxy._todo_controller.snapshot("tc1"))

    # Worker-local update with unknown IDs should be absorbed, not forwarded
    worker_local = [{"description": "Worker-local TODO", "status": "active"}]
    emitted_before = len(emitted)
    proxy._relay_worker_todo_update("tc1", worker_local)

    # No new emission for unknown worker-local tasks
    assert len(emitted) == emitted_before

    # The only emitted tasks are canonical
    for _, tasks in emitted:
        for task in tasks:
            assert task.get("description") != "Worker-local TODO"
            assert task.get("id") == "step-1"


def test_dispatch_to_worker_schema_uses_compact_capsule_not_file_plan():
    schema_text = _dispatch_schema_text()
    schema_text_lower = schema_text.lower()

    assert "file-by-file implementation plan" not in schema_text_lower
    assert "complete implementation handoff" not in schema_text_lower
    assert "complete, self-contained implementation handoff" not in schema_text_lower
    assert "compact, self-contained worker task capsule" in schema_text_lower
    assert "call this tool instead of continuing to discuss or expand a plan" in schema_text_lower
    assert "the planner's deliverable for implementation work is this tool call" in schema_text_lower


def test_dispatch_to_worker_schema_capsule_stays_self_contained():
    schema_text = _dispatch_schema_text().lower()
    properties = DISPATCH_TOOL_DEF["function"]["parameters"]["properties"]

    for phrase in (
        "target seam",
        "files allowed",
        "behavior to preserve",
        "constraints / non-goals",
        "validation commands",
        "summary",
        "work order and dispatch it",
    ):
        assert phrase in schema_text

    for field in (
        "target_regions",
        "validation_commands",
        "non_goals",
        "allowed_responsibilities",
        "forbidden_responsibilities",
        "expected_public_symbols",
        "expected_dataclass_fields",
        "forbidden_public_methods",
        "forbidden_calls",
    ):
        assert field in properties


def _run_worker_dispatch_with_context_metadata(
    tmp_path: Path,
    req: WorkerDispatchRequest,
    *,
    tool_call_id: str = "context_call",
):
    from unittest.mock import Mock

    from aura.bridge.dispatch import _DispatchPending, _DispatchProxy
    from aura.client.events import Done
    from aura.conversation.tools import ToolRegistry
    from aura.hooks import hooks

    def worker_backend(**_kwargs):
        return iter(
            [
                Done(
                    finish_reason="stop",
                    full_message={
                        "role": "assistant",
                        "content": "Done.",
                        "reasoning_content": None,
                    },
                )
            ]
        )

    previous_worker_hook = hooks._handlers.get("generate_worker_code")
    hooks.unregister("generate_worker_code")
    hooks.register("generate_worker_code", worker_backend)
    try:
        proxy = _DispatchProxy(
            parent_widget=Mock(),
            registry_factory=lambda _mode: ToolRegistry(
                workspace_root=tmp_path,
                mode="worker",
            ),
            approval_proxy=Mock(),
            workspace_root=tmp_path,
        )
        result = proxy._run_worker(tool_call_id, req, _DispatchPending(req))
        return result, proxy
    finally:
        hooks.unregister("generate_worker_code")
        if previous_worker_hook is not None:
            hooks.register("generate_worker_code", previous_worker_hook)


def _seed_graduated_hazard_for_dispatch(
    workspace_root: Path,
    *,
    task_kind: str = "bugfix",
    target_file: str = "aura/context_gearbox/sources.py",
) -> None:
    from aura.hazard.capture import record_hazard

    for index in range(3):
        record_hazard(
            workspace_root=workspace_root,
            model="test-model",
            status="validation_failed",
            structured_failure={"failure_class": "pytest_failure"},
            target_files=[target_file],
            task_shape={"task_kind": task_kind},
            errors=["AssertionError: context source was skipped"],
            tool_call_id=f"dispatch-{index}",
        )


def test_dispatch_request_to_from_dict_roundtrip():
    original = WorkerDispatchRequest(
        goal="Add docstrings",
        files=["module.py", "utils.py"],
        spec="Add Google-style docstrings to all public functions.",
        acceptance="All public functions have docstrings.",
    )
    data = original.to_dict()
    restored = WorkerDispatchRequest.from_dict(data)
    assert restored.goal == original.goal
    assert restored.files == original.files
    assert restored.spec == original.spec
    assert restored.acceptance == original.acceptance


def test_dispatch_request_from_dict_missing_fields():
    """Missing fields should default to empty strings/lists."""
    req = WorkerDispatchRequest.from_dict({})
    assert req.goal == ""
    assert req.files == []
    assert req.spec == ""
    assert req.acceptance == ""


def test_dispatch_request_from_dict_files_not_list():
    """If 'files' is not a list, should default to empty list."""
    req = WorkerDispatchRequest.from_dict({"files": "not_a_list"})
    assert req.files == []


def test_dispatch_request_from_dict_files_with_non_strings():
    """Non-string entries in files should be converted to strings."""
    req = WorkerDispatchRequest.from_dict({"files": [123, None, "ok.py"]})
    assert req.files == ["123", "None", "ok.py"]


# WorkerDispatchResult

def test_dispatch_result_success():
    result = WorkerDispatchResult(ok=True, summary="Done.", cancelled=False)
    assert result.ok is True
    assert result.summary == "Done."
    assert result.cancelled is False


def test_dispatch_result_cancelled():
    result = WorkerDispatchResult(ok=False, summary="User cancelled.", cancelled=True)
    assert result.ok is False
    assert result.cancelled is True


def test_dispatch_result_to_tool_payload():
    result = WorkerDispatchResult(ok=True, summary="Success", cancelled=False)
    payload = result.to_tool_payload()
    assert payload == {"ok": True, "cancelled": False, "summary": "Success"}


def test_dispatch_result_to_tool_payload_with_extras():
    result = WorkerDispatchResult(
        ok=True, summary="Done", cancelled=False,
        extras={"diff": "+added line", "commit": "abc1234"},
    )
    payload = result.to_tool_payload()
    assert payload["ok"] is True
    assert payload["extras"]["diff"] == "+added line"
    assert payload["extras"]["commit"] == "abc1234"


def test_dispatch_result_to_tool_payload_empty_extras():
    """Empty extras dict should not appear in payload."""
    result = WorkerDispatchResult(ok=True, summary="Done", cancelled=False, extras={})
    payload = result.to_tool_payload()
    assert "extras" not in payload


def test_dispatch_result_followup_fields_roundtrip():
    result = WorkerDispatchResult(
        ok=False,
        summary="Worker reached the pass limit.",
        needs_followup=True,
        phase_boundary=True,
        followup_reason="worker_tool_call_limit_reached",
        recoverable=True,
        completed=["Read target files"],
        remaining=["Run validation"],
        modified_files=["aura/example.py"],
        validation="Not run",
        suggested_next_spec="Run a validation-only pass.",
    )

    payload = result.to_tool_payload()
    restored = WorkerDispatchResult.from_tool_payload(payload)

    assert payload["needs_followup"] is True
    assert payload["phase_boundary"] is True
    assert restored.recoverable is True
    assert restored.completed == ["Read target files"]
    assert restored.remaining == ["Run validation"]
    assert restored.modified_files == ["aura/example.py"]
    assert restored.validation == "Not run"
    assert restored.suggested_next_spec == "Run a validation-only pass."


def test_dispatch_result_from_legacy_payload():
    restored = WorkerDispatchResult.from_tool_payload(
        {"ok": True, "cancelled": False, "summary": "Done"}
    )

    assert restored.ok is True
    assert restored.summary == "Done"
    assert restored.needs_followup is False
    assert restored.completed == []


def test_normalize_outcome_status_accepts_enum_and_valid_strings():
    assert normalize_outcome_status(WorkerOutcomeStatus.validation_failed) == "validation_failed"
    assert normalize_outcome_status(" validation_failed ") == "validation_failed"
    assert normalize_outcome_status("not_a_status") is None
    assert normalize_outcome_status(None) is None


def test_dispatch_result_from_tool_payload_drops_unknown_status():
    restored = WorkerDispatchResult.from_tool_payload(
        {"ok": False, "summary": "legacy", "status": "not_a_status"}
    )

    assert restored.status is None


def test_infer_outcome_status_legacy_non_ok_defaults_to_followup():
    result = WorkerDispatchResult(ok=False, summary="Something failed.")

    assert infer_outcome_status(result) == WorkerOutcomeStatus.needs_followup.value


def test_infer_outcome_status_uses_explicit_harness_signals():
    result = WorkerDispatchResult(
        ok=False,
        summary="Harness error - RuntimeError: boom",
        extras={"worker_internal_error": True},
    )

    assert infer_outcome_status(result) == WorkerOutcomeStatus.harness_error.value


# Structured fields roundtrips


def test_dispatch_request_structured_fields_roundtrip():
    original = WorkerDispatchRequest(
        goal="Refactor auth",
        files=["auth.py"],
        spec="Extract auth logic into its own module.",
        acceptance="All tests pass.",
        summary="Planner summary",
        allowed_responsibilities=["refactoring"],
        forbidden_responsibilities=["new features"],
        required_outputs=["auth.py"],
        validation_commands=["pytest tests/ -x"],
        risk_notes=["auth is critical"],
        non_goals=["rewrite from scratch"],
        expected_dataclass_fields={"MyClass": ["a", "b"]},
    )
    data = original.to_dict()
    restored = WorkerDispatchRequest.from_dict(data)
    assert restored.goal == original.goal
    assert restored.files == original.files
    assert restored.spec == original.spec
    assert restored.acceptance == original.acceptance
    assert restored.summary == original.summary
    assert restored.allowed_responsibilities == original.allowed_responsibilities
    assert restored.forbidden_responsibilities == original.forbidden_responsibilities
    assert restored.required_outputs == original.required_outputs
    assert restored.validation_commands == original.validation_commands
    assert restored.risk_notes == original.risk_notes
    assert restored.non_goals == original.non_goals
    assert restored.expected_dataclass_fields == {"MyClass": ["a", "b"]}


def test_dispatch_request_structured_fields_default_to_empty():
    """Missing structured fields should default to empty lists/dicts."""
    req = WorkerDispatchRequest.from_dict(
        {"goal": "x", "files": [], "spec": "", "acceptance": ""}
    )
    assert req.allowed_responsibilities == []
    assert req.forbidden_responsibilities == []
    assert req.required_outputs == []
    assert req.validation_commands == []
    assert req.risk_notes == []
    assert req.non_goals == []
    assert req.expected_dataclass_fields == {}


def test_dispatch_request_structured_fields_not_list():
    """Non-list structured fields should default to empty lists/dicts."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "allowed_responsibilities": "not_a_list",
            "forbidden_responsibilities": None,
            "required_outputs": 123,
            "validation_commands": "pytest",
            "risk_notes": {},
            "non_goals": 0,
        }
    )
    assert req.allowed_responsibilities == []
    assert req.forbidden_responsibilities == []
    assert req.required_outputs == []
    assert req.validation_commands == []
    assert req.risk_notes == []
    assert req.non_goals == []


def test_dispatch_request_expected_dataclass_fields_dict():
    """expected_dataclass_fields as dict roundtrips correctly."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "expected_dataclass_fields": {"MyModel": ["id", "name"]},
        }
    )
    assert req.expected_dataclass_fields == {"MyModel": ["id", "name"]}


def test_dispatch_request_expected_dataclass_fields_list_old_format():
    """expected_dataclass_fields as list (old format) degrades to {}."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "expected_dataclass_fields": ["a", "b"],
        }
    )
    assert req.expected_dataclass_fields == {}


def test_dispatch_request_expected_dataclass_fields_none():
    """expected_dataclass_fields as None degrades to {}."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "expected_dataclass_fields": None,
        }
    )
    assert req.expected_dataclass_fields == {}


def test_dispatch_request_target_regions_roundtrip_and_cleaning():
    original = WorkerDispatchRequest(
        goal="Scope edit",
        files=["aura/foo.py"],
        spec="Update the helper.",
        acceptance="py_compile passes.",
        target_regions=[
            {
                "path": "aura/foo.py",
                "symbol": "Builder.build",
                "start_line": "10",
                "end_line": 20,
                "note": "update validation",
                "ignored": "value",
            },
            "not a dict",
            {"path": ["bad"], "start_line": 0, "end_line": "x"},
        ],
    )

    data = original.to_dict()
    restored = WorkerDispatchRequest.from_dict(data)

    assert data["target_regions"] == [
        {
            "path": "aura/foo.py",
            "symbol": "Builder.build",
            "note": "update validation",
            "start_line": 10,
            "end_line": 20,
        }
    ]
    assert restored.target_regions == data["target_regions"]
    assert WorkerDispatchRequest.from_dict({"target_regions": {"path": "x.py"}}).target_regions == []


def test_worker_task_spec_target_regions_roundtrip():
    original = WorkerTaskSpec(
        goal="Scope edit",
        files=["aura/foo.py"],
        target_regions=[
            {
                "path": "aura/foo.py",
                "symbol": "build_prompt",
                "start_line": 80,
                "end_line": "130",
                "note": "replace stale handoff text",
            }
        ],
    )

    data = original.to_dict()
    restored = WorkerTaskSpec.from_dict(data)

    assert data["target_regions"] == [
        {
            "path": "aura/foo.py",
            "symbol": "build_prompt",
            "note": "replace stale handoff text",
            "start_line": 80,
            "end_line": 130,
        }
    ]
    assert restored.target_regions == data["target_regions"]


def test_task_shape_infers_new_tool_or_app():
    shape = infer_task_shape(
        goal="Build a release tracker app",
        spec="Create a workflow that stores releases and shows useful results.",
        files=["aura/release_tracker.py"],
    )

    assert shape.task_kind == "new_tool_or_app"
    assert "configure/create the thing" in shape.product_flow
    assert "job/task" in shape.state_concepts
    assert "partial run failure" in shape.failure_modes
    assert shape.extension_seams
    assert "build the smallest shippable slice, not a demo" in shape.quality_pressure
    assert "no fake integrations" in shape.forbidden_moves
    assert "release_tracker" in shape.likely_entities


def test_task_shape_old_payload_without_new_fields_loads_safely():
    shape = TaskShape.from_dict(
        {
            "task_kind": "new_tool_or_app",
            "core_flow": ["run the main action"],
            "quality_pressure": ["build the smallest shippable slice, not a demo"],
        }
    )

    assert shape.task_kind == "new_tool_or_app"
    assert shape.product_flow == ["run the main action"]
    assert shape.state_concepts == []


def test_task_shape_infers_bugfix():
    shape = infer_task_shape(goal="Fix failing dispatch validation regression")

    assert shape.task_kind == "bugfix"
    assert "make the fix surgical" in shape.quality_pressure


def test_task_shape_infers_gui_polish():
    shape = infer_task_shape(goal="Polish the dialog button copy and layout")

    assert shape.task_kind == "gui_polish"
    assert "clarify user states, copy, and layout" in shape.quality_pressure


def test_task_shape_infers_cleanup_and_refactor():
    cleanup = infer_task_shape(goal="Cleanup dead code in the old parser")
    refactor = infer_task_shape(goal="Refactor auth helpers to extract token parsing")

    assert cleanup.task_kind == "cleanup"
    assert refactor.task_kind == "refactor"


def test_task_shape_unknown_fallback():
    shape = infer_task_shape(goal="Adjust the thing")

    assert shape.task_kind == "unknown"
    assert shape.proof_targets


def test_dispatch_request_task_shape_roundtrip():
    original = WorkerDispatchRequest(
        goal="Build a dashboard",
        files=["dashboard.py"],
        spec="Create the dashboard.",
        acceptance="py_compile passes.",
        task_shape=infer_task_shape(goal="Build a dashboard app", files=["dashboard.py"]),
    )

    data = original.to_dict()
    restored = WorkerDispatchRequest.from_dict(data)

    assert "task_shape" in data
    assert restored.task_shape is not None
    assert restored.task_shape.task_kind == "new_tool_or_app"
    assert "smallest shippable slice" in " ".join(restored.task_shape.quality_pressure)


def test_dispatch_request_old_payload_without_task_shape_loads():
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "Fix a bug",
            "files": ["a.py"],
            "spec": "Fix it.",
            "acceptance": "py_compile passes.",
        }
    )

    assert req.task_shape is None
    assert "task_shape" not in req.to_dict()


def test_dispatch_request_malformed_task_shape_falls_back_safely():
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "Build a tracker app",
            "files": ["tracker.py"],
            "spec": "Build it.",
            "acceptance": "py_compile passes.",
            "task_shape": ["not", "a", "dict"],
        }
    )

    assert req.task_shape is not None
    assert req.task_shape.task_kind == "unknown"


def test_normalize_worker_task_task_shape_inference_fails_open(monkeypatch):
    req = WorkerDispatchRequest(
        goal="Build a tracker app",
        files=["tracker.py"],
        spec="Create it.",
        acceptance="py_compile passes.",
    )

    def broken_infer(**kwargs):
        raise RuntimeError("shape inference failed")

    monkeypatch.setattr("aura.conversation.dispatch.infer_task_shape", broken_infer)

    task = normalize_worker_task(req)

    assert task.task_shape is not None
    assert task.task_shape.task_kind == "unknown"
    assert isinstance(getattr(task.task_shape, "_task_shape_ms", None), float)


def test_rg_no_match_validation_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "",
            "command": 'rg "show_response" app/tray.py',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_rg_pipeline_no_match_validation_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "",
            "command": 'rg "show_response" app/tray.py | rg "No recent"',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_rg_pipeline_no_match_validation_preview_shape_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output_preview": "",
            "command": 'rg "show_response" app/tray.py | rg "No recent"',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_grep_no_match_validation_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "",
            "command": 'grep -R "show_response" app/',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_py_compile_exit_1_validation_remains_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "SyntaxError: invalid syntax",
            "command": "python -m py_compile app/tray.py",
        }
    ]

    assert _unrecovered_validation_failures(results) == results


def test_python3_py_compile_terminal_record_counts_as_validation():
    assert _is_validation_terminal_record(
        {
            "command": "python3 -m py_compile app/tray.py",
            "ok": True,
            "exit_code": 0,
        }
    )


def test_workspace_file_exists_resolves_under_root_and_blocks_escape(tmp_path):
    root = tmp_path / "workspace"
    target = root / "aura" / "bridge" / "dispatch.py"
    target.parent.mkdir(parents=True)
    target.write_text("pass\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")

    exists = _workspace_file_exists(root)

    assert exists("./aura/bridge/dispatch.py")
    assert exists(r".\aura\bridge\dispatch.py")
    assert not exists("../outside.py")


def test_rg_real_error_validation_remains_unrecovered_failure():
    exit_2 = {
        "ok": False,
        "exit_code": 2,
        "output": "",
        "command": 'rg "unterminated" app/tray.py',
    }
    error_output = {
        "ok": False,
        "exit_code": 1,
        "output": "regex parse error: unclosed group",
        "command": 'rg "(" app/tray.py',
    }

    assert _unrecovered_validation_failures([exit_2]) == [exit_2]
    assert _unrecovered_validation_failures([error_output]) == [error_output]


def test_arbitrary_exit_1_validation_remains_unrecovered_failure():
    result = {
        "ok": False,
        "exit_code": 1,
        "output": "",
        "command": 'python -c "raise SystemExit(1)"',
    }

    assert _unrecovered_validation_failures([result]) == [result]


def test_worker_prompt_guides_search_validation_semantics():
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="Check it.",
    )

    prompt = _format_spec_as_user_message(req)

    assert "Task Shape Contract" in prompt
    assert "Task shape: unknown" in prompt
    assert "Goal" in prompt
    assert "Do things" in prompt
    assert "Files" in prompt
    assert "- x.py" in prompt
    assert "Builder Note" in prompt
    assert "Do it." in prompt
    assert "Acceptance / Validation" in prompt
    assert "Check it." in prompt
    assert "Begin." in prompt
    assert "pytest" not in prompt


def test_worker_task_formatting_includes_target_regions():
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["aura/foo.py"],
        spec="Do it.",
        acceptance="Check it.",
        target_regions=[
            {
                "path": "aura/foo.py",
                "symbol": "ClassName.method",
                "start_line": 120,
                "end_line": 170,
                "note": "update validation handling",
            },
            {
                "path": "aura/foo.py",
                "symbol": "build_prompt",
                "note": "scope around the prompt assembly helper",
            },
            {
                "path": "aura/foo.py",
                "start_line": 80,
                "end_line": 130,
                "note": "replace stale handoff text",
            },
        ],
    )

    prompt = _format_spec_as_user_message(req)

    assert prompt.index("Files") < prompt.index("Target Regions") < prompt.index("Builder Note")
    assert "- aura/foo.py :: ClassName.method lines 120-170 — update validation handling" in prompt
    assert "- aura/foo.py :: build_prompt — scope around the prompt assembly helper" in prompt
    assert "- aura/foo.py lines 80-130 — replace stale handoff text" in prompt


def test_worker_task_formatting_bugfix_uses_compact_task_shape_contract():
    req = WorkerDispatchRequest(
        goal="Fix failing import behavior",
        files=["x.py"],
        spec="Repair the broken import path.",
        acceptance="Run `python -m py_compile x.py`.",
        task_shape=TaskShape(task_kind="bugfix"),
    )

    prompt = _format_spec_as_user_message(req)

    assert "Task Shape Contract" in prompt
    assert "Task shape: bugfix" in prompt
    assert "- surgical fix" in prompt
    assert "- preserve compatibility" in prompt
    assert "- prove changed behavior" in prompt
    assert "Implementation standard" not in prompt
    assert "Core flow:" not in prompt


def test_worker_task_formatting_new_tool_or_app_uses_task_shape_contract():
    req = WorkerDispatchRequest(
        goal="Build a scout dashboard app",
        files=["scout_dashboard.py"],
        spec="Create the dashboard with saved state and result review.",
        acceptance="Run `python -m py_compile scout_dashboard.py`.",
    )

    prompt = _format_spec_as_user_message(req)

    assert "Task Shape Contract" in prompt
    assert "Task shape: new_tool_or_app" in prompt
    assert "Core flow:" in prompt
    assert "- configure/create the thing" in prompt
    assert "State concepts:" in prompt
    assert "- result/candidate" in prompt
    assert "Quality traps:" in prompt
    assert "- no fake integrations" in prompt
    assert "Proof intent:" in prompt
    assert "- prove the core flow runs or is directly exercised" in prompt
    assert "Implementation standard" not in prompt


def test_lone_rg_terminal_result_does_not_count_as_task_validation():
    terminal_results = [
        {
            "ok": True,
            "exit_code": 0,
            "output": "match",
            "output_preview": "match",
            "command": 'rg "needle" app/tray.py',
        }
    ]

    assert _validation_results_for_task([], terminal_results, []) == []


def test_explicit_validation_command_counts_from_terminal_results():
    terminal_results = [
        {
            "ok": True,
            "exit_code": 0,
            "output": "match",
            "output_preview": "match",
            "command": 'rg "needle" app/tray.py && exit 1 || exit 0',
        }
    ]

    assert _validation_results_for_task(
        [],
        terminal_results,
        ['rg "needle" app/tray.py && exit 1 || exit 0'],
    ) == terminal_results


def test_validation_selector_commands_merge_after_planner_commands():
    planner_command = "python -m pytest tests/test_info_hub_pane.py -q"
    selector = _build_worker_validation_selector_plan(
        changed_files=["aura/gui/info_hub_pane.py"],
        task_kind="gui_polish",
        context_gearbox={},
        workspace_root=None,
    )

    combined = _combine_validation_commands(
        [planner_command],
        _validation_selector_commands(selector),
    )

    assert combined[0] == planner_command
    assert combined.count(planner_command) == 1
    assert "python -m py_compile aura/gui/info_hub_pane.py" in combined
    assert "python -m ruff check aura/gui/info_hub_pane.py" in combined


def test_validation_selector_changed_files_ignore_deleted_writes():
    relay = SimpleNamespace(
        write_results=[
            {
                "applied": True,
                "deleted": True,
                "path": "aura/gui/removed.py",
            }
        ],
        touched_files={"aura/gui/removed.py"},
    )

    assert _validation_selector_changed_files(relay) == []


def test_worker_dispatch_feeds_selector_commands_into_explicit_validation_path(tmp_path):
    from unittest.mock import Mock, patch

    from aura.bridge.dispatch import _DispatchPending, _DispatchProxy
    from aura.client import ToolResult
    from aura.conversation.dispatch import WorkerDispatchRequest

    planner_command = "python -m pytest tests/test_info_hub_pane.py -q"
    captured: dict[str, list[str]] = {}

    class FakeConversationManager:
        def __init__(self, history, _registry):
            self.history = history

        def send(self, **kwargs):
            commands = kwargs["explicit_validation_commands"]
            captured["initial"] = list(commands)
            kwargs["on_event"](
                ToolResult(
                    tool_call_id="write1",
                    name="patch_file",
                    ok=True,
                    result=json.dumps(
                        {
                            "ok": True,
                            "applied": True,
                            "path": "aura/gui/info_hub_pane.py",
                        }
                    ),
                )
            )
            captured["after_write"] = list(commands)
            self.history.append_assistant(
                {
                    "role": "assistant",
                    "content": "Done.",
                    "reasoning_content": None,
                }
            )

    registry = Mock()
    registry.workspace_root = tmp_path
    registry.mode = "worker"
    registry.set_contract = Mock()
    registry.set_task_shape = Mock()

    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=lambda _mode: registry,
        approval_proxy=Mock(),
        workspace_root=tmp_path,
    )
    req = WorkerDispatchRequest(
        goal="Update info hub validation line",
        files=["aura/gui/info_hub_pane.py"],
        spec="Make the change.",
        acceptance="Run focused validation.",
        validation_commands=[planner_command],
    )

    with (
        patch("aura.bridge.dispatch.ConversationManager", FakeConversationManager),
        patch("aura.conversation.persistence.save_dispatch_record_to_memory"),
        patch("aura.hazard.capture.record_hazard"),
    ):
        result = proxy._run_worker("selector_call", req, _DispatchPending(req))

    after_write = captured["after_write"]
    assert captured["initial"] == [planner_command]
    assert after_write[0] == planner_command
    assert after_write.count(planner_command) == 1
    assert "python -m py_compile aura/gui/info_hub_pane.py" in after_write
    assert "python -m ruff check aura/gui/info_hub_pane.py" in after_write

    selector = result.extras["validation_selector"]
    assert planner_command in selector["commands"]
    assert "python -m py_compile aura/gui/info_hub_pane.py" in selector["commands"]
    assert "python -m ruff check aura/gui/info_hub_pane.py" in selector["commands"]
    assert selector["display"].startswith("Validation plan:")
    assert "python -m" not in selector["display"]
    assert "ruff" not in selector["display"].lower()


def test_final_report_partial_suite_with_py_compile_is_not_harness_failure():
    assert _final_report_claims_failure("Could not run full test suite, but py_compile passed")
    assert _final_report_claims_validation("Could not run full test suite, but py_compile passed")

    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [{"tool": "edit_file", "path": "a.py"}],
        [],
        {},
        ["Worker final report mentioned possible blocker, failed validation, or incomplete verification."],
    )

    assert not summary.startswith("Harness error")
    assert "Worker completed with caveats" in summary


def test_no_blocker_remains_does_not_trip_failure_sniffing():
    assert not _final_report_claims_failure("No blocker remains. py_compile passed.")


def test_not_tested_does_not_count_as_validation_success():
    assert not _final_report_claims_validation("Changed the file. Not tested.")


def test_raw_rg_passed_does_not_count_as_validation_success():
    assert not _final_report_claims_validation('rg "old" app/tray.py passed')


def test_summary_renders_needs_planner_resolution_label():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [],
        [],
        {"status": "needs_planner_resolution"},
        [],
        status="needs_planner_resolution",
    )

    assert "Worker needs Planner resolution" in summary
    assert "Planner will revise the handoff" in summary
    assert not summary.startswith("Harness error")


def test_structured_worker_failure_summary_is_not_harness_error():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [],
        ["blocked by missing dependency (worker_blocked)."],
        {},
        [],
    )

    assert "Worker needs follow-up" in summary
    assert not summary.startswith("Harness error")


# normalize_worker_task


def test_normalize_worker_task_validation_commands_explicit():
    """Explicit validation_commands on the request should be forwarded directly."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="pytest tests/",
        validation_commands=["ruff check .", "py_compile x.py"],
    )
    spec = normalize_worker_task(req)
    assert spec.validation_commands == ["ruff check .", "py_compile x.py"]


def test_normalize_worker_task_validation_commands_fallback():
    """Empty validation_commands but acceptance has a backtick-quoted command."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="Run `pytest tests/test_x.py -v` to check.",
        validation_commands=[],
    )
    spec = normalize_worker_task(req)
    assert "pytest tests/test_x.py -v" in spec.validation_commands


def test_normalize_worker_task_non_goals_explicit():
    """Explicit non_goals should be forwarded, not parsed from spec."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="## Non-Goals\n- Parse spec\n- Rewrite",
        acceptance="",
        non_goals=["explicit", "only"],
    )
    spec = normalize_worker_task(req)
    assert spec.non_goals == ["explicit", "only"]


def test_normalize_worker_task_structured_fields():
    """allowed_responsibilities, forbidden_responsibilities, required_outputs, risk_notes, expected_dataclass_fields forwarded."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="Check it.",
        allowed_responsibilities=["editing"],
        forbidden_responsibilities=["new files"],
        required_outputs=["x.py"],
        risk_notes=["breaks easily"],
        expected_dataclass_fields={"MyClass": ["a"]},
    )
    spec = normalize_worker_task(req)
    assert spec.allowed_responsibilities == ["editing"]
    assert spec.forbidden_responsibilities == ["new files"]
    assert spec.required_outputs == ["x.py"]
    assert spec.risk_notes == ["breaks easily"]
    assert spec.contract is not None
    assert spec.contract.expected_dataclass_fields == {"MyClass": ["a"]}


def test_normalize_worker_task_carries_target_regions():
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="Check it.",
        target_regions=[{"path": "x.py", "symbol": "do_thing", "start_line": "12"}],
    )

    spec = normalize_worker_task(req)

    assert spec.target_regions == [{"path": "x.py", "symbol": "do_thing", "start_line": 12}]


# New focused tests for dispatch and approval proxy timeout and cancellation

def test_dispatch_proxy_timeout():
    from unittest.mock import Mock

    from aura.bridge.dispatch import _DispatchProxy
    from aura.conversation.dispatch import WorkerDispatchRequest

    # Create approval proxy mock
    approval = Mock()
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=approval,
    )

    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["test.py"],
        spec="test spec",
        acceptance="test acceptance",
    )

    # We want to patch DISPATCH_TIMEOUT or call it under thread or short timeout so it doesn't block for 300s in test.
    # Let's temporarily override DISPATCH_TIMEOUT in the module!
    import aura.bridge.dispatch
    orig_timeout = aura.bridge.dispatch.DISPATCH_TIMEOUT
    aura.bridge.dispatch.DISPATCH_TIMEOUT = 0.05  # 50ms for fast test execution!
    try:
        res = proxy.request_dispatch("test_call_id", req)
        assert res.ok is False
        assert res.recoverable is True
        assert "Plan expired" in res.summary
        assert "UI signal" not in res.summary
        assert res.extras.get("dispatch_not_started") is True
        assert res.extras.get("dispatch_approval_timeout") is True
    finally:
        aura.bridge.dispatch.DISPATCH_TIMEOUT = orig_timeout


def test_dispatch_proxy_stale_dispatch():
    from unittest.mock import Mock

    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=Mock(),
    )

    # If we call user_dispatched with a tool_call_id that is not in pending:
    res = proxy.user_dispatched("stale_call_id", "goal", [], "spec", "acceptance", "")
    assert res is False

    # Same for user_cancelled:
    res = proxy.user_cancelled("stale_call_id")
    assert res is False



def test_dispatch_proxy_cancel_all_unblocks_and_cancels_active_dialog():
    import threading
    import time
    from unittest.mock import Mock

    from aura.bridge.dispatch import _DispatchProxy
    from aura.conversation.dispatch import WorkerDispatchRequest

    approval = Mock()
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=approval,
    )

    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["test.py"],
        spec="test spec",
        acceptance="test acceptance",
    )

    # Let's trigger a dispatch wait in a background thread
    results = []
    def runner():
        res = proxy.request_dispatch("test_call_id", req)
        results.append(res)

    t = threading.Thread(target=runner)
    t.start()

    # Wait a tiny bit to ensure the thread is waiting
    time.sleep(0.05)

    # Now cancel all pending
    proxy.cancel_all_pending()
    t.join(timeout=1.0)

    # Verify that cancel_active_dialog was called on the approval proxy
    approval.cancel_active_dialog.assert_called_once()

    # Verify that request_dispatch returned a cancelled result
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].cancelled is True
    assert results[0].summary == "Cancelled"
    assert results[0].extras.get("dispatch_not_started") is True
    assert results[0].extras.get("dispatch_cancelled") is True


def test_worker_dispatch_metadata_includes_context_gearbox(tmp_path):
    req = WorkerDispatchRequest(
        goal="Inspect context metadata",
        files=["aura/drones/runner.py"],
        spec="Inspect only.",
        acceptance="No validation required.",
    )

    result, proxy = _run_worker_dispatch_with_context_metadata(tmp_path, req)

    context_gearbox = result.extras.get("context_gearbox")
    assert isinstance(context_gearbox, dict)
    assert {"summary", "ledger"}.issubset(context_gearbox)
    assert context_gearbox["eviction"]["would_evict_count"] == 0
    assert context_gearbox["summary"]["display"].startswith("Context: ")
    assert any(
        entry["source_id"] == "planner_dispatch_contract"
        and entry["included"] is False
        for entry in context_gearbox["ledger"]
    )
    assert any(
        entry["source_id"] == "skill_pack"
        and entry["included"] is True
        and entry["reason"] == "terrain-selected skills for this context"
        for entry in context_gearbox["ledger"]
    )
    assert proxy.result_metadata("context_call")["extras"]["context_gearbox"] == context_gearbox

    encoded = json.dumps(context_gearbox, sort_keys=True)
    assert "Worker role:" not in encoded
    assert "Core kernel:" not in encoded
    assert "Done." not in encoded


def test_worker_dispatch_metadata_loads_user_authored_skill_by_task_content(tmp_path):
    authored_dir = tmp_path / ".aura" / "skills" / "authored"
    skill_dir = authored_dir / "auth_token_refresh"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "When editing authentication token refresh, "
            "validate session renewal behavior."
        ),
        encoding="utf-8",
    )
    req = WorkerDispatchRequest(
        goal="Fix auth token refresh regression",
        files=["app.py"],
        spec="Repair the login flow.",
        acceptance="Session renewal still works.",
        task_shape=TaskShape(task_kind="bugfix"),
    )

    result, _proxy = _run_worker_dispatch_with_context_metadata(
        tmp_path,
        req,
        tool_call_id="authored_context_call",
    )

    context_gearbox = result.extras.get("context_gearbox")
    assert isinstance(context_gearbox, dict)
    assert "skill_pack" in context_gearbox["summary"]["loaded"]
    assert any(
        entry["kind"] == "individual_skill" and entry["included"] is True
        for entry in context_gearbox["ledger"]
    )


def test_worker_dispatch_metadata_loads_bundled_skills_via_path_overlap(tmp_path):
    _seed_graduated_hazard_for_dispatch(tmp_path)
    req = WorkerDispatchRequest(
        goal="Inspect context metadata",
        files=["aura/context_gearbox/sources.py"],
        spec="Inspect only.",
        acceptance="No validation required.",
        task_shape=TaskShape(task_kind="bugfix"),
    )

    result, _proxy = _run_worker_dispatch_with_context_metadata(
        tmp_path,
        req,
        tool_call_id="hazard_context_call",
    )

    context_gearbox = result.extras.get("context_gearbox")
    assert isinstance(context_gearbox, dict)
    assert "skill_pack" in context_gearbox["summary"]["loaded"]
    assert any(
        entry["source_id"] == "skill_pack"
        and entry["included"] is True
        and entry["kind"] == "skill_pack"
        for entry in context_gearbox["ledger"]
    )


def test_approval_proxy_active_dialog_cancellation():
    from unittest.mock import Mock, patch

    from aura.bridge.approval_proxy import _ApprovalProxy
    from aura.conversation.tools import ApprovalRequest

    proxy = _ApprovalProxy(parent_widget=Mock())
    assert proxy._active_dialog is None

    # Let's mock DiffApprovalDialog
    with patch("aura.bridge.approval_proxy.DiffApprovalDialog") as mock_dlg_cls:
        mock_dlg = Mock()
        mock_dlg_cls.return_value = mock_dlg

        # When dlg.exec() is called, we will simulate calling cancel_active_dialog
        def fake_exec():
            # While exec is running, the active_dialog must be set
            assert proxy._active_dialog is mock_dlg
            proxy.cancel_active_dialog()
            return 0

        mock_dlg.exec.side_effect = fake_exec
        mock_dlg.decision.return_value = Mock(action="reject")

        # Now trigger the open_dialog Slot directly (which is what BlockingQueuedConnection invokes)
        proxy._last_request = ApprovalRequest(
            tool_name="write_file",
            rel_path="a.py",
            old_content="old",
            new_content="new",
            is_new_file=False,
        )
        proxy._open_dialog()

        # After execution completes, active_dialog should be None again
        assert proxy._active_dialog is None


def test_dispatch_proxy_cancelled_before_start():
    import threading
    import time
    from unittest.mock import Mock

    from aura.bridge.dispatch import DISPATCH_TIMEOUT, _DispatchProxy
    from aura.conversation.dispatch import WorkerDispatchRequest

    approval = Mock()
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=approval,
    )

    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["test.py"],
        spec="test spec",
        acceptance="test acceptance",
    )

    results = []

    def runner():
        res = proxy.request_dispatch("test_call_id", req)
        results.append(res)

    t = threading.Thread(target=runner)
    t.start()

    # Wait briefly for the thread to start waiting on decision_event
    time.sleep(0.05)

    # Simulate user cancelling before timeout
    proxy.user_cancelled("test_call_id")
    t.join(timeout=DISPATCH_TIMEOUT + 1)

    assert len(results) == 1
    res = results[0]
    assert res.ok is False
    assert res.cancelled is True
    assert res.summary == "Cancelled"
    assert res.extras.get("dispatch_not_started") is True
    assert res.extras.get("dispatch_cancelled") is True


# WorkerDispatchResult — completion semantics tests


def test_worker_result_ok_false_when_writes_but_no_validation():
    """A Worker that writes files but runs no validation must NOT be ok."""
    result = WorkerDispatchResult(
        ok=False, summary="Modified files but no validation",
        needs_followup=True, recoverable=True,
        modified_files=["test.py"],
        extras={"caveats": ["Worker modified files but ran no validation command."]},
    )
    assert not result.ok
    assert result.needs_followup
    assert result.recoverable
    payload = result.to_tool_payload()
    assert payload["ok"] is False
    assert payload.get("needs_followup") is True
    assert payload.get("recoverable") is True


def test_worker_result_ok_false_when_failed_write():
    """A Worker with a failed edit_file must NOT be ok."""
    result = WorkerDispatchResult(
        ok=False, summary="Write tool failed",
        extras={"errors": ["Worker write tool 'edit_file' reported failure."]},
    )
    assert not result.ok
    payload = result.to_tool_payload()
    assert payload["ok"] is False


def test_worker_result_ok_false_when_failed_validation():
    """A Worker with failed terminal validation must NOT be ok."""
    result = WorkerDispatchResult(
        ok=False, summary="Validation failed",
        needs_followup=True, recoverable=True,
        extras={"errors": ["Validation command failed (exit code 1): python -m pytest"]},
    )
    assert not result.ok
    assert result.needs_followup
    assert result.recoverable
    payload = result.to_tool_payload()
    assert payload["ok"] is False


def test_worker_result_ok_true_when_writes_and_passing_validation():
    """A Worker that writes AND validates successfully must BE ok."""
    result = WorkerDispatchResult(
        ok=True, summary="Wrote files and validated",
        needs_followup=False,
        modified_files=["test.py"],
    )
    assert result.ok
    payload = result.to_tool_payload()
    assert payload["ok"] is True


def test_modified_files_are_derived_from_applied_write_receipts_only():
    writes = [
        {"path": "changed.py", "applied": True, "write_outcome": "applied"},
        {"path": "removed.py", "applied": True, "deleted": True, "write_outcome": "deleted"},
        {"path": "changed.py", "applied": True, "write_outcome": "applied"},
        {"path": "claimed.py", "applied": False, "write_outcome": "not_applied_craft_rejected"},
        {"path": ".aura/tmp/_check_acceptance.py", "applied": True, "write_outcome": "applied"},
    ]

    assert _applied_modified_files(writes) == ["changed.py", "removed.py"]


def test_worker_summary_dedupes_duplicate_modified_file_rows_and_count():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [
            {"tool": "apply_edit_transaction", "path": "a.py", "applied": True, "is_new_file": False},
            {"tool": "apply_edit_transaction", "path": "a.py", "applied": True, "is_new_file": False},
            {"tool": "write_file", "path": "b.py", "applied": True, "is_new_file": True},
            {"tool": "delete_file", "path": "old.py", "applied": True, "deleted": True},
        ],
        [],
        {},
        [],
        validation_results=[{"command": "python -m py_compile a.py b.py", "ok": True, "exit_code": 0}],
    )

    assert "Files changed   : 3 (1 edited, 1 new, 1 deleted)" in summary
    assert summary.count("a.py   (edit)") == 1
    assert summary.count("b.py   (new)") == 1
    assert summary.count("old.py   (deleted)") == 1


def test_worker_summary_dedupes_duplicate_failed_writes():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [],
        [],
        {},
        [],
        not_applied_writes=[
            {"path": "_tmp_inspect_wear.py", "failure_class": "introduced_environment_issue"},
            {"path": "_tmp_inspect_wear.py", "failure_class": "introduced_environment_issue"},
        ],
    )

    assert summary.count("_tmp_inspect_wear.py") == 1


def test_worker_summary_separates_validated_command_from_validation_issue():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [{"tool": "patch_file", "path": "a.py", "applied": True, "is_new_file": False}],
        [],
        {},
        [],
        validation_results=[
            {
                "command": "python -m pytest tests/test_worker_summary_card.py -k worker_summary_card_inserted_by_default -x",
                "ok": True,
                "exit_code": 0,
                "counts_as_validation": True,
                "counts_as_product_failure": False,
            }
        ],
        validation_command_issues=[
            {
                "command": "python -m pytest tests/test_worker_summary_card.py -k worker_summary_card_inserted_by_default -x",
                "validation_raw_text": "python -m pytest tests/test_worker_summary_card.py -k worker_summary_card_inserted_by_default -x passes",
                "classification": "passed",
                "expected_outcome": "passes",
                "normalized": True,
                "normalization_reason": "trailing outcome prose token",
                "counts_as_product_failure": False,
            }
        ],
        status=WorkerOutcomeStatus.completed_with_caveats.value,
    )

    assert "Validated:" in summary
    assert "Validation command issues:" in summary
    assert "Product failures:" not in summary
    assert "trailing prose token `passes`" in summary


def test_scratch_diagnostic_missing_import_is_caveat_not_project_patch():
    relay = SimpleNamespace(
        write_results=[
            {"tool": "write_file", "path": "_tmp_inspect_wear.py", "applied": True, "is_new_file": True},
        ],
        touched_files={"_tmp_inspect_wear.py"},
        wrote_new_files=["_tmp_inspect_wear.py"],
        edited_existing_files=[],
        not_applied_writes=[
            {
                "path": "_tmp_inspect_wear.py",
                "failure_class": "introduced_environment_issue",
                "introduced_environment_issues": [
                    {"message": "Import source 'numpy' could not be resolved in workspace or stdlib."}
                ],
            },
            {
                "path": "_tmp_inspect_wear.py",
                "failure_class": "introduced_environment_issue",
                "introduced_environment_issues": [
                    {"message": "Import source 'numpy' could not be resolved in workspace or stdlib."}
                ],
            },
        ],
        failed_tool_results=[
            {
                "name": "write_file",
                "path": "_tmp_inspect_wear.py",
                "failure_class": "introduced_environment_issue",
                "introduced_environment_issues": [
                    {"message": "Import source 'numpy' could not be resolved in workspace or stdlib."}
                ],
            }
        ],
        terminal_results=[
            {
                "command": "python _tmp_inspect_wear.py",
                "ok": False,
                "exit_code": 1,
                "output": "ModuleNotFoundError: No module named 'numpy'",
                "output_preview": "ModuleNotFoundError: No module named 'numpy'",
            }
        ],
    )

    assert _diagnostic_environment_caveats(relay) == [
        "Diagnostic script could not run because numpy is not installed in the project environment."
    ]

    _filter_scratch_write_records(relay)

    assert relay.write_results == []
    assert relay.not_applied_writes == []
    assert relay.failed_tool_results == []
    assert relay.touched_files == set()


def test_scratch_py_compile_validation_rows_are_not_project_validation():
    validation_results = [
        {"command": "python -m py_compile _tmp_inspect_baked.py", "ok": True, "exit_code": 0},
        {"command": "python -m py_compile aura/config.py", "ok": True, "exit_code": 0},
    ]

    filtered = _filter_scratch_validation_results(validation_results)

    assert filtered == [validation_results[1]]
    assert _is_validation_scratch_path("_tmp_inspect_baked.py")
    assert _is_validation_scratch_path(".aura/tmp/_tmp_inspect_baked.py")


def test_worker_result_needs_followup_not_terminal():
    """WorkerDispatchResult with ok=False and recoverable=True should not be terminal."""
    result = WorkerDispatchResult(
        ok=False, summary="Needs another pass",
        needs_followup=True, recoverable=True,
    )
    assert not result.ok
    assert result.needs_followup
    assert result.recoverable
    # Test roundtrip
    payload = result.to_tool_payload()
    restored = WorkerDispatchResult.from_tool_payload(payload)
    assert not restored.ok
    assert restored.needs_followup
    assert restored.recoverable


# ── _unrecovered_not_applied_writes path normalization ──────────────


def test_unrecovered_not_applied_cleared_by_same_normalized_path():
    """A failed write on .gitignore is cleared by a later success on ./.gitignore."""
    tool_results = [
        {"name": "patch_file", "path": ".gitignore", "applied": False, "ok": False,
         "failure_class": "patch_file_hash_mismatch"},
        {"name": "write_file", "path": "./.gitignore", "applied": True, "ok": True},
    ]
    assert _unrecovered_not_applied_writes(tool_results) == []


def test_unrecovered_not_applied_cleared_by_windows_backslash_path():
    """Windows backslash path .\\.gitignore normalizes to .gitignore and clears failure."""
    tool_results = [
        {"name": "patch_file", "path": ".gitignore", "applied": False, "ok": False,
         "failure_class": "patch_file_hash_mismatch"},
        {"name": "write_file", "path": ".\\\\.gitignore", "applied": True, "ok": True},
    ]
    assert _unrecovered_not_applied_writes(tool_results) == []


def test_unrecovered_not_applied_cleared_by_repeated_dot_segments():
    tool_results = [
        {"name": "patch_file", "path": ".gitignore", "applied": False, "ok": False,
         "failure_class": "patch_file_hash_mismatch"},
        {"name": "write_file", "path": "././.gitignore", "applied": True, "ok": True},
    ]

    assert _unrecovered_not_applied_writes(tool_results) == []


def test_unrecovered_not_applied_remains_for_different_path():
    """A failed write on .gitignore is NOT cleared by success on README.md."""
    tool_results = [
        {"name": "patch_file", "path": ".gitignore", "applied": False, "ok": False,
         "failure_class": "patch_file_hash_mismatch"},
        {"name": "write_file", "path": "README.md", "applied": True, "ok": True},
    ]
    result = _unrecovered_not_applied_writes(tool_results)
    assert len(result) == 1
    assert _normalize_worker_path(result[0]["path"]) == ".gitignore"


def test_unrecovered_not_applied_remains_when_no_later_write():
    """A failed write stays unresolved when no later write touches its path."""
    tool_results = [
        {"name": "patch_file", "path": ".gitignore", "applied": False, "ok": False,
         "failure_class": "patch_file_hash_mismatch"},
    ]
    result = _unrecovered_not_applied_writes(tool_results)
    assert len(result) == 1


def test_unrecovered_not_applied_cleared_by_delete_on_same_path():
    """A failed write is cleared by a successful delete on the same path."""
    tool_results = [
        {"name": "patch_file", "path": "tmp.py", "applied": False, "ok": False,
         "failure_class": "patch_file_hash_mismatch"},
        {"name": "delete_file", "path": "tmp.py", "applied": True, "deleted": True, "ok": True},
    ]
    assert _unrecovered_not_applied_writes(tool_results) == []


def test_worker_summary_not_applied_writes_uses_unresolved():
    """_build_worker_summary should not show failed writes that were later resolved."""
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=[".gitignore"], spec="spec", acceptance=""),
        History(),
        [{"tool": "write_file", "path": ".gitignore", "applied": True, "is_new_file": False}],
        [],
        {},
        [],
        not_applied_writes=[],  # representing that unresolved list was empty
    )
    assert "Failed writes:" not in summary


def test_worker_summary_keeps_unresolved_failed_write_action_visible():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=[".gitignore"], spec="spec", acceptance=""),
        History(),
        [],
        [],
        {},
        [],
        not_applied_writes=[
            {"path": ".gitignore", "failure_class": "patch_file_hash_mismatch"},
        ],
        status=WorkerOutcomeStatus.edit_mechanics_blocked.value,
    )

    assert "Failed writes:" in summary
    assert ".gitignore" in summary
    assert "Action needed   : Re-dispatch" in summary
    assert "Action needed   : None" not in summary


def test_worker_summary_resolved_failed_write_has_no_failed_section_or_action():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=[".gitignore"], spec="spec", acceptance=""),
        History(),
        [{"tool": "patch_file", "path": ".gitignore", "applied": True, "is_new_file": False}],
        [],
        {},
        [],
        not_applied_writes=[],
        status=WorkerOutcomeStatus.completed.value,
    )

    assert "Failed writes:" not in summary
    assert "Action needed   : None" in summary


def test_craft_rejected_summary_is_not_worker_failed():
    req = WorkerDispatchRequest(goal="Fix code", files=["a.py"], spec="spec", acceptance="")
    summary = _build_worker_summary(
        req,
        History(),
        [],
        ["Craft blocked generated Python."],
        {},
        [],
        status=WorkerOutcomeStatus.craft_rejected.value,
    )

    assert "Craft rejected" in summary
    assert "Worker failed" not in summary


def _status_for(**overrides):
    args = {
        "ok": False,
        "needs_followup": True,
        "recoverable": True,
        "has_internal_failure": False,
        "has_validation_failure": False,
        "has_recoverable_edit_blocker": False,
        "has_source_inspection_blocker": False,
        "has_no_work": False,
        "has_no_progress_failure": False,
        "is_implementation": True,
        "has_unverified_acceptance": False,
        "has_hard_failure": True,
        "has_applied_writes": False,
        "result_errors": ["failed"],
        "result_caveats": [],
        "continuation": {},
        "structured_failure": {},
        "write_failures": [],
    }
    args.update(overrides)
    return _compute_outcome_status(**args)


def test_compute_outcome_status_distinguishes_craft_blocked_and_rejected():
    assert _status_for(
        structured_failure={"failure_class": "approval_rejected"}
    ) == WorkerOutcomeStatus.approval_rejected.value
    assert _status_for(
        write_failures=[{"failure_class": "craft_blocked"}]
    ) == WorkerOutcomeStatus.craft_blocked.value
    assert _status_for(
        write_failures=[{"failure_class": "craft_rejected"}]
    ) == WorkerOutcomeStatus.craft_rejected.value
    assert _status_for(
        write_failures=[{"reject": True}]
    ) == WorkerOutcomeStatus.craft_rejected.value


def test_compute_outcome_status_maps_craft_not_applied_to_craft_status():
    status = _status_for(
        has_recoverable_edit_blocker=True,
        write_failures=[
            {
                "path": "a.py",
                "applied": False,
                "write_outcome": "not_applied_craft_rejected",
                "failure_class": "craft_blocked",
            }
        ],
    )

    assert status == WorkerOutcomeStatus.craft_blocked.value


def test_compute_outcome_status_distinguishes_validation_mechanics_and_harness():
    assert _status_for(
        has_recoverable_edit_blocker=True
    ) == WorkerOutcomeStatus.edit_mechanics_blocked.value
    assert _status_for(
        write_failures=[{"failure_class": "edit_transaction_symbol_not_found"}]
    ) == WorkerOutcomeStatus.edit_mechanics_blocked.value
    assert _status_for(
        has_validation_failure=True
    ) == WorkerOutcomeStatus.validation_failed.value
    assert _status_for(
        has_source_inspection_blocker=True,
        result_errors=["Terminal source inspection was blocked."],
    ) == WorkerOutcomeStatus.needs_followup.value
    assert _status_for(
        has_internal_failure=True
    ) == WorkerOutcomeStatus.harness_error.value


def test_compute_outcome_status_completed_with_caveats_requires_ok():
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        result_caveats=["minor caveat"],
    ) == WorkerOutcomeStatus.completed_with_caveats.value


def test_recovered_write_failure_does_not_cause_edit_mechanics_blocked():
    """A worker that recovered from a failed edit and applied writes should NOT be edit_mechanics_blocked."""
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        write_failures=[{"failure_class": "edit_transaction_not_applicable"}],
        has_applied_writes=True,
    ) != WorkerOutcomeStatus.edit_mechanics_blocked.value
    # With writes applied and no hard errors, should be completed
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        write_failures=[{"failure_class": "edit_transaction_not_applicable"}],
        has_applied_writes=True,
    ) == WorkerOutcomeStatus.completed.value


def test_unrecovered_write_failure_after_prior_write_blocks_completion():
    assert _status_for(
        ok=False,
        needs_followup=True,
        recoverable=True,
        has_hard_failure=False,
        result_errors=[],
        has_recoverable_edit_blocker=True,
        has_applied_writes=True,
        write_failures=[{"failure_class": "edit_mechanics_old_str_not_found"}],
    ) == WorkerOutcomeStatus.edit_mechanics_blocked.value


def test_validation_not_run_after_files_changed_is_needs_followup():
    assert _status_for(
        ok=False,
        needs_followup=True,
        recoverable=True,
        has_hard_failure=False,
        result_errors=[],
        has_applied_writes=True,
        has_unverified_acceptance=True,
        result_caveats=["Files changed but validation did not run."],
    ) == WorkerOutcomeStatus.needs_followup.value


def test_environment_caveat_is_not_edit_mechanics_failure():
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        has_applied_writes=True,
        result_caveats=["Pre-existing environment issue on a.py: yaml import missing"],
        write_failures=[{"failure_class": "pre_existing_environment_issue"}],
    ) == WorkerOutcomeStatus.completed_with_caveats.value


# WorkerMismatch


def test_worker_mismatch_roundtrip():
    original = WorkerMismatch(
        kind=WorkerMismatch.MISSING_SYMBOL,
        file_paths=["src/module.py"],
        requested="Add function compute_value",
        observed="Symbol compute_value already exists in another module",
        worker_recommendation="Use a different name or merge the implementations",
        question_for_planner="Should I rename or merge?",
    )
    data = original.to_dict()
    restored = WorkerMismatch.from_dict(data)
    assert restored is not None
    assert restored.kind == original.kind
    assert restored.file_paths == original.file_paths
    assert restored.requested == original.requested
    assert restored.observed == original.observed
    assert restored.worker_recommendation == original.worker_recommendation
    assert restored.question_for_planner == original.question_for_planner


def test_worker_mismatch_from_dict_none():
    assert WorkerMismatch.from_dict(None) is None


def test_worker_mismatch_from_dict_non_dict():
    assert WorkerMismatch.from_dict(42) is None
    assert WorkerMismatch.from_dict("not a dict") is None
    assert WorkerMismatch.from_dict([]) is None


def test_worker_mismatch_from_dict_missing_fields():
    restored = WorkerMismatch.from_dict({"kind": "missing_symbol"})
    assert restored is not None
    assert restored.kind == "missing_symbol"
    assert restored.file_paths == []
    assert restored.requested == ""
    assert restored.observed == ""
    assert restored.worker_recommendation == ""
    assert restored.question_for_planner == ""


def test_worker_mismatch_from_dict_coerces_types():
    restored = WorkerMismatch.from_dict(
        {
            "kind": 42,
            "file_paths": "single_path.py",
            "requested": None,
            "observed": 123,
            "worker_recommendation": True,
            "question_for_planner": ["not", "a", "string"],
        }
    )
    assert restored is not None
    assert restored.kind == "42"
    assert restored.file_paths == ["single_path.py"]
    assert restored.requested == "None"
    assert restored.observed == "123"
    assert restored.worker_recommendation == "True"
    assert restored.question_for_planner == "['not', 'a', 'string']"


# WorkerDispatchResult with mismatch


def test_dispatch_result_to_tool_payload_includes_mismatch():
    mismatch = WorkerMismatch(
        kind=WorkerMismatch.SCHEMA_MISMATCH,
        file_paths=["config.yaml"],
        requested="Add field timeout=30",
        observed="config.yaml uses different schema version",
        worker_recommendation="Update schema version first",
        question_for_planner="Should I update schema version?",
    )
    result = WorkerDispatchResult(
        ok=False,
        summary="Mismatch detected",
        mismatch=mismatch,
    )
    payload = result.to_tool_payload()
    assert "mismatch" in payload
    assert payload["mismatch"]["kind"] == "schema_mismatch"
    assert payload["mismatch"]["file_paths"] == ["config.yaml"]


def test_dispatch_result_from_tool_payload_restores_mismatch():
    payload = {
        "ok": False,
        "summary": "Mismatch",
        "mismatch": {
            "kind": "conflicting_spec",
            "file_paths": ["a.py", "b.py"],
            "requested": "Add feature X",
            "observed": "Feature X conflicts with existing auth",
            "worker_recommendation": "Disable auth override for X",
            "question_for_planner": "Should I disable auth?",
        },
    }
    restored = WorkerDispatchResult.from_tool_payload(payload)
    assert restored.mismatch is not None
    assert restored.mismatch.kind == "conflicting_spec"
    assert restored.mismatch.file_paths == ["a.py", "b.py"]
    assert restored.mismatch.requested == "Add feature X"


def test_dispatch_result_no_mismatch_omitted():
    result = WorkerDispatchResult(ok=True, summary="All good")
    payload = result.to_tool_payload()
    assert "mismatch" not in payload


def test_dispatch_result_existing_payload_no_mismatch():
    payload = {"ok": True, "summary": "Old style"}
    restored = WorkerDispatchResult.from_tool_payload(payload)
    assert restored.mismatch is None


# normalize_outcome_status with needs_planner_resolution


def test_normalize_needs_planner_resolution():
    assert (
        normalize_outcome_status("needs_planner_resolution")
        == WorkerOutcomeStatus.needs_planner_resolution.value
    )
    assert (
        normalize_outcome_status(WorkerOutcomeStatus.needs_planner_resolution)
        == WorkerOutcomeStatus.needs_planner_resolution.value
    )


# infer_outcome_status with mismatch


def test_infer_from_mismatch():
    mismatch = WorkerMismatch(
        kind=WorkerMismatch.MISSING_SYMBOL,
        file_paths=["x.py"],
        requested="Add func",
        observed="Symbol exists",
        worker_recommendation="Rename",
        question_for_planner="Rename?",
    )
    result = WorkerDispatchResult(
        ok=False, summary="Conflict", mismatch=mismatch
    )
    assert infer_outcome_status(result) == WorkerOutcomeStatus.needs_planner_resolution.value


def test_infer_from_extras_planner_resolution_needed():
    result = WorkerDispatchResult(
        ok=False,
        summary="Need planner input",
        extras={"planner_resolution_needed": True},
    )
    assert infer_outcome_status(result) == WorkerOutcomeStatus.needs_planner_resolution.value


def test_infer_explicit_status_wins_over_mismatch():
    mismatch = WorkerMismatch(
        kind=WorkerMismatch.MISSING_SYMBOL,
        file_paths=["x.py"],
        requested="Add func",
        observed="Symbol exists",
        worker_recommendation="Rename",
        question_for_planner="Rename?",
    )
    result = WorkerDispatchResult(
        ok=True, summary="Success", mismatch=mismatch,
        status=WorkerOutcomeStatus.completed.value,
    )
    assert infer_outcome_status(result) == WorkerOutcomeStatus.completed.value


# WorkerMismatch kind constants


def test_worker_mismatch_kind_constants():
    assert WorkerMismatch.MISSING_SYMBOL == "missing_symbol"
    assert WorkerMismatch.SCHEMA_MISMATCH == "schema_mismatch"
    assert WorkerMismatch.CONFLICTING_SPEC == "conflicting_spec"
    assert WorkerMismatch.AMBIGUOUS_PRODUCT_DECISION == "ambiguous_product_decision"
    assert WorkerMismatch.REPEATED_EDIT_FAILURE == "repeated_edit_failure"
    assert WorkerMismatch.VALIDATION_UNCLEAR == "validation_unclear"


class TestMismatchDispatchFlow:
    """Tests for mismatch -> Planner resolution dispatch control-flow."""

    def test_mismatch_allows_planner_continuation(self):
        """_failed_dispatch_allows_planner_continuation returns True for mismatch."""
        from aura.conversation.dispatch_failure import _failed_dispatch_allows_planner_continuation
        mismatch = WorkerMismatch(
            kind=WorkerMismatch.MISSING_SYMBOL,
            file_paths=["a.py"],
            requested="Add func",
            observed="Symbol already exists",
            worker_recommendation="Rename",
            question_for_planner="Should I rename?",
        )
        result = WorkerDispatchResult(
            ok=False, summary="Mismatch", mismatch=mismatch
        )
        assert _failed_dispatch_allows_planner_continuation(result)

    def test_planner_resolution_needed_extra_allows_continuation(self):
        """_failed_dispatch_allows_planner_continuation returns True for extras flag."""
        from aura.conversation.dispatch_failure import _failed_dispatch_allows_planner_continuation
        result = WorkerDispatchResult(
            ok=False,
            summary="Need planner",
            extras={"planner_resolution_needed": True},
        )
        assert _failed_dispatch_allows_planner_continuation(result)

    def test_explicit_status_allows_continuation_without_mismatch_extras(self):
        """Explicit status=needs_planner_resolution allows continuation with no mismatch/extras."""
        from aura.conversation.dispatch_failure import _failed_dispatch_allows_planner_continuation
        result = WorkerDispatchResult(
            ok=False,
            summary="Need planner",
            status=WorkerOutcomeStatus.needs_planner_resolution.value,
        )
        assert result.mismatch is None
        assert not result.extras
        assert _failed_dispatch_allows_planner_continuation(result)

    def test_mismatch_extras_in_payload(self):
        """to_tool_payload includes planner_resolution_needed etc. when mismatch exists."""
        mismatch = WorkerMismatch(
            kind=WorkerMismatch.CONFLICTING_SPEC,
            file_paths=["a.py", "b.py"],
            requested="Add feature X",
            observed="Feature X conflicts with Y",
            worker_recommendation="Disable Y",
            question_for_planner="Should I disable Y?",
        )
        result = WorkerDispatchResult(
            ok=False, summary="Conflict", mismatch=mismatch
        )
        payload = result.to_tool_payload()
        assert payload["extras"]["planner_resolution_needed"] is True
        assert payload["extras"]["mismatch_kind"] == "conflicting_spec"
        assert payload["extras"]["mismatch_question"] == "Should I disable Y?"

    def test_no_mismatch_no_extra_extras(self):
        """Normal result without mismatch doesn't get those extras injected."""
        result = WorkerDispatchResult(
            ok=False, summary="Regular failure",
            extras={"some_other": "data"},
        )
        payload = result.to_tool_payload()
        assert "extras" in payload
        assert "planner_resolution_needed" not in payload["extras"]
        assert "mismatch_kind" not in payload["extras"]
        assert "mismatch_question" not in payload["extras"]
        assert payload["extras"]["some_other"] == "data"

    def test_ok_result_no_extras_without_mismatch(self):
        """Result without mismatch and empty extras gets no extras in payload."""
        result = WorkerDispatchResult(ok=True, summary="All good")
        payload = result.to_tool_payload()
        assert "extras" not in payload


class TestMismatchParsing:
    """Tests for _parse_continuation_report and _parse_structured_worker_failure mismatch handling."""

    def test_parse_continuation_report_mismatch_json(self):
        """<mismatch> containing a JSON object parses correctly."""
        content = (
            "<status>needs_followup</status>"
            "<mismatch>{\"kind\": \"missing_symbol\", \"file_paths\": [\"a.py\"], "
            "\"requested\": \"Add func\", \"observed\": \"Symbol exists\", "
            "\"worker_recommendation\": \"Rename\", "
            "\"question_for_planner\": \"Rename?\"}</mismatch>"
            "<completed>Done</completed>"
        )
        result = _parse_continuation_report(content)
        assert result["mismatch"] is not None
        assert result["mismatch"]["kind"] == "missing_symbol"
        assert result["mismatch"]["file_paths"] == ["a.py"]
        assert result["mismatch"]["question_for_planner"] == "Rename?"

    def test_parse_continuation_report_no_mismatch(self):
        """Content without mismatch returns None for mismatch key."""
        content = (
            "<status>ok</status>"
            "<completed>Task done</completed>"
        )
        result = _parse_continuation_report(content)
        assert result["mismatch"] is None

    def test_parse_continuation_report_mismatch_non_json(self):
        """Mismatch text that isn't JSON is stored as {"raw": ...}."""
        content = (
            "<status>needs_followup</status>"
            "<mismatch>Worker found a conflict but didn't format as JSON</mismatch>"
        )
        result = _parse_continuation_report(content)
        assert result["mismatch"] is not None
        assert result["mismatch"]["raw"] == "Worker found a conflict but didn't format as JSON"

    def test_parse_structured_worker_failure_mismatch(self):
        """JSON with status needs_planner_resolution and mismatch dict is recognized."""
        data = {
            "status": "needs_planner_resolution",
            "mismatch": {
                "kind": "conflicting_spec",
                "file_paths": ["a.py"],
                "requested": "Add X",
                "observed": "X conflicts with Y",
                "worker_recommendation": "",
                "question_for_planner": "Disable Y?",
            },
        }
        content = json.dumps(data)
        result = _parse_structured_worker_failure(content)
        assert result.get("status") == "needs_planner_resolution"
        assert result["mismatch"]["kind"] == "conflicting_spec"

    def test_compute_outcome_status_mismatch(self):
        """_compute_outcome_status with mismatch status returns needs_planner_resolution."""
        continuation = {"status": "needs_planner_resolution"}
        outcome = _compute_outcome_status(
            ok=False,
            needs_followup=False,
            recoverable=False,
            has_internal_failure=False,
            has_validation_failure=False,
            has_recoverable_edit_blocker=False,
            has_source_inspection_blocker=False,
            has_no_work=False,
            has_no_progress_failure=False,
            is_implementation=True,
            has_unverified_acceptance=False,
            has_hard_failure=False,
            result_errors=[],
            result_caveats=[],
            continuation=continuation,
        )
        assert outcome == WorkerOutcomeStatus.needs_planner_resolution.value

    def test_mismatch_does_not_break_existing_parsing(self):
        """A normal continuation report still parses correctly after mismatch changes."""
        content = (
            "<status>needs_followup</status>"
            "<reason>Validation errors</reason>"
            "<completed>- Fixed test_a.py\n- Updated test_b.py</completed>"
            "<remaining>- Fix test_c.py</remaining>"
            "<recommended_next_step>Run tests again</recommended_next_step>"
        )
        result = _parse_continuation_report(content)
        assert result["status"] == "needs_followup"
        assert result["reason"] == "Validation errors"
        assert result["completed"] == ["Fixed test_a.py", "Updated test_b.py"]
        assert result["remaining"] == ["Fix test_c.py"]
        assert result["recommended_next_step"] == "Run tests again"
        assert result["mismatch"] is None


# ── Validation success override tests ──────────────────────────────────


class TestValidationSuccessOverride:
    """Passing validation with applied writes must not become needs_followup
    due to weak final wording."""

    def test_passing_validation_with_writes_is_completed_with_caveats(self):
        """Applied writes + passing validation → completed_with_caveats, not needs_followup."""
        assert _status_for(
            ok=True,
            needs_followup=False,
            recoverable=False,
            has_hard_failure=False,
            result_errors=[],
            result_caveats=[
                "Worker final report did not clearly mention validation or acceptance verification."
            ],
            has_applied_writes=True,
            has_unverified_acceptance=True,
        ) == WorkerOutcomeStatus.completed_with_caveats.value

    def test_no_writes_unverified_acceptance_is_needs_followup(self):
        """No writes + unverified acceptance → needs_followup."""
        assert _status_for(
            ok=False,
            needs_followup=True,
            recoverable=True,
            has_hard_failure=False,
            result_errors=[],
            result_caveats=[
                "Worker final report did not clearly mention validation or acceptance verification."
            ],
            has_applied_writes=False,
            has_unverified_acceptance=True,
        ) == WorkerOutcomeStatus.needs_followup.value

    def test_validation_never_ran_is_needs_followup_even_with_writes(self):
        """Files changed but validation never ran → needs_followup."""
        assert _status_for(
            ok=False,
            needs_followup=True,
            recoverable=True,
            has_hard_failure=False,
            result_errors=[],
            result_caveats=["Files changed but validation did not run."],
            has_applied_writes=True,
            has_unverified_acceptance=True,
        ) == WorkerOutcomeStatus.needs_followup.value

    def test_passing_validation_with_writes_no_caveats_is_completed(self):
        """Applied writes + passing validation + no caveats → completed."""
        assert _status_for(
            ok=True,
            needs_followup=False,
            recoverable=False,
            has_hard_failure=False,
            result_errors=[],
            result_caveats=[],
            has_applied_writes=True,
            has_unverified_acceptance=False,
        ) == WorkerOutcomeStatus.completed.value


# ── Worker TODO isolation tests ────────────────────────────────────────


class TestWorkerTodoIsolation:
    """Worker-local TODO updates must not replace the canonical checklist."""

    def test_worker_update_todo_list_during_canonical_cannot_replace_checklist(self):
        """Worker calling update_todo_list during canonical dispatch cannot
        replace the visible checklist rows."""
        from unittest.mock import Mock

        from aura.bridge.dispatch import _DispatchProxy
        from aura.bridge.todo_controller import DispatchTodoController

        proxy = _DispatchProxy(
            parent_widget=Mock(),
            registry_factory=Mock(),
            approval_proxy=Mock(),
            workspace_root=None,
        )

        # Set up canonical state
        proxy._todo_controller.begin("tc1", [
            {"id": "plan-a", "description": "Planned task A"},
            {"id": "plan-b", "description": "Planned task B"},
        ])

        emitted = []
        proxy.workerTodoListUpdated.connect(lambda tid, tasks: emitted.append((tid, list(tasks))))

        # Worker emits a completely different TODO list
        worker_tasks = [
            {"id": "worker-x", "description": "Worker's own task X", "status": "active"},
            {"id": "worker-y", "description": "Worker's own task Y", "status": "pending"},
        ]
        proxy._relay_worker_todo_update("tc1", worker_tasks)

        # Only the canonical snapshot should be visible (emitted if status changed)
        for _, tasks in emitted:
            ids = [t["id"] for t in tasks]
            assert ids == ["plan-a", "plan-b"]
            for task in tasks:
                assert task["id"] in ("plan-a", "plan-b")
                assert task["description"] in ("Planned task A", "Planned task B")

    def test_worker_progress_known_id_updates_status_only(self):
        """Worker update for a known objective ID may update status only."""
        from unittest.mock import Mock

        from aura.bridge.dispatch import _DispatchProxy

        proxy = _DispatchProxy(
            parent_widget=Mock(),
            registry_factory=Mock(),
            approval_proxy=Mock(),
            workspace_root=None,
        )

        proxy._todo_controller.begin("tc1", [
            {"id": "step-1", "description": "Step 1"},
        ])

        emitted = []
        proxy.workerTodoListUpdated.connect(lambda tid, tasks: emitted.append((tid, list(tasks))))

        # Worker marks step-1 as done
        proxy._relay_worker_todo_update("tc1", [
            {"id": "step-1", "status": "done"},
        ])

        assert len(emitted) >= 1
        _, tasks = emitted[-1]
        assert tasks[0]["status"] == "done"
        assert tasks[0]["description"] == "Step 1"

    def test_no_canonical_state_worker_update_passes_through(self):
        """When no canonical state exists, worker TODO updates pass through normally."""
        from unittest.mock import Mock

        from aura.bridge.dispatch import _DispatchProxy

        proxy = _DispatchProxy(
            parent_widget=Mock(),
            registry_factory=Mock(),
            approval_proxy=Mock(),
            workspace_root=None,
        )

        emitted = []
        proxy.workerTodoListUpdated.connect(lambda tid, tasks: emitted.append((tid, list(tasks))))

        worker_tasks = [{"description": "Worker task", "status": "active"}]
        proxy._relay_worker_todo_update("tc-no-canonical", worker_tasks)

        assert len(emitted) == 1
        assert emitted[0][1] == worker_tasks

    def test_canonical_survives_after_session_run(self):
        """After session.run() returns, the canonical TODO state stays visible
        so late Worker-local events cannot repaint the rail."""
        from types import SimpleNamespace

        from aura.bridge.dispatch_session import DispatchSession
        from aura.bridge.todo_controller import DispatchTodoController
        from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
        from aura.conversation.dispatch_plan import WorkerStepSpec, plan_from_request

        steps = [
            WorkerStepSpec(id="s1", title="Step 1", goal="First step", files=["a.py"]),
        ]
        req = WorkerDispatchRequest(
            goal="Campaign", files=["a.py"], spec="", acceptance="", steps=steps,
        )
        plan = plan_from_request(req)
        controller = DispatchTodoController()

        def run_worker_step(_tid, _req, _pending):
            return WorkerDispatchResult(ok=True, summary="done")

        session = DispatchSession(
            tool_call_id="tc1",
            original_request=req,
            plan=plan,
            run_worker_step=run_worker_step,
            pending=SimpleNamespace(),
            todo_controller=controller,
        )
        session.run()

        # After session.run(), canonical state still exists
        assert controller.has_canonical("tc1") is True
        snapshot = controller.snapshot("tc1")
        assert len(snapshot) == 1
        assert snapshot[0]["status"] == "done"

        # A late worker-local update cannot replace the checklist
        result = controller.absorb_worker_update("tc1", [
            {"id": "worker-adhoc", "description": "Late worker task", "status": "active"},
        ])
        assert result is None  # nothing changed, no emission
        snapshot = controller.snapshot("tc1")
        assert len(snapshot) == 1
        assert snapshot[0]["id"] == "s1"

    def test_new_dispatch_clears_prior_canonical(self):
        """Starting a new dispatch for the same tool_call_id clears prior canonical state."""
        from unittest.mock import Mock

        from aura.bridge.dispatch import _DispatchProxy

        proxy = _DispatchProxy(
            parent_widget=Mock(),
            registry_factory=Mock(),
            approval_proxy=Mock(),
            workspace_root=None,
        )

        # Set up prior canonical state
        proxy._todo_controller.begin("tc1", [
            {"id": "old-step", "description": "Old step"},
        ])

        # The clear happens at the start of request_dispatch.
        # Simulate that behavior:
        proxy._todo_controller.clear("tc1")

        assert proxy._todo_controller.has_canonical("tc1") is False
        assert proxy._todo_controller.snapshot("tc1") == []
