from types import SimpleNamespace

from aura.bridge.dispatch_session import DispatchSession
from aura.bridge.dispatch_todo_controller import DispatchTodoController
from aura.bridge.worker_report import _format_spec_as_user_message
from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch_plan import (
    WorkerDispatchPlan,
    WorkerStepSpec,
    plan_from_request,
)
from aura.conversation.dispatch_todo_manifest import DispatchTodoItem
from aura.conversation.worker_outcome import WorkerOutcomeStatus


def _request_with_steps() -> WorkerDispatchRequest:
    return WorkerDispatchRequest(
        goal="Extract shell pipeline helpers.",
        files=["aura/bridge/worker_completion_result.py", "aura/bridge/_shell_pipeline.py"],
        spec="Extract shell pipeline helpers and wire the existing caller.",
        acceptance="Helpers are extracted, caller imports them, and validation passes.",
        summary="Extract shell pipeline helpers.",
        steps=[
            WorkerStepSpec(
                id="step-1",
                title="Create shell pipeline helper module",
                goal="Move helper functions into aura/bridge/_shell_pipeline.py.",
                spec="Create the helper module with extracted functions.",
                files=["aura/bridge/_shell_pipeline.py"],
                acceptance="The helper module contains the extracted functions.",
            ),
            WorkerStepSpec(
                id="step-2",
                title="Wire helper module into completion result",
                goal="Import and use the extracted helpers from worker_completion_result.py.",
                spec="Remove local helper definitions and import them from the helper module.",
                files=["aura/bridge/worker_completion_result.py"],
                acceptance="The caller imports the helper module and validation passes.",
            ),
        ],
    )


def _session_callbacks(events: list[tuple]) -> dict:
    return {
        "begin_steps": lambda tool_id, objectives: events.append(
            ("begin", tool_id, [item["id"] for item in objectives])
        ),
        "set_active_step": lambda tool_id, step_id: events.append(
            ("active", tool_id, step_id)
        ),
        "mark_step_done": lambda tool_id, step_id: events.append(
            ("done", tool_id, step_id)
        ),
        "finish_steps": lambda tool_id: events.append(("finish_steps", tool_id)),
        "emit_worker_started": lambda tool_id: events.append(("started", tool_id)),
        "emit_worker_finished": lambda tool_id, ok, summary, needs_followup, status: events.append(
            ("finished", tool_id, ok, needs_followup, status)
        ),
    }


def test_nonfinal_progress_followup_continues_same_dispatch_session():
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    calls = []
    events = []
    results = [
        WorkerDispatchResult(
            ok=False,
            summary="Created helper module; full validation waits for the wiring step.",
            needs_followup=True,
            recoverable=True,
            status=WorkerOutcomeStatus.validation_failed.value,
            modified_files=["aura/bridge/_shell_pipeline.py"],
            extras={
                "writes": [
                    {"path": "aura/bridge/_shell_pipeline.py", "applied": True}
                ],
                "validation_results": [
                    {
                        "command": "python -m compileall aura/bridge",
                        "ok": False,
                    }
                ],
            },
        ),
        WorkerDispatchResult(
            ok=True,
            summary="Wired helper module and validation passed.",
            status=WorkerOutcomeStatus.completed.value,
            modified_files=["aura/bridge/worker_completion_result.py"],
        ),
    ]

    def run_step(tool_id, step_req, pending):
        calls.append((tool_id, step_req.goal))
        return results[len(calls) - 1]

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        **_session_callbacks(events),
    )

    result = session.run()

    assert result.ok is True
    assert calls == [
        ("call_dispatch", req.steps[0].goal),
        ("call_dispatch", req.steps[1].goal),
    ]
    assert [event for event in events if event[0] == "started"] == [
        ("started", "call_dispatch")
    ]
    assert [event for event in events if event[0] == "finished"] == [
        ("finished", "call_dispatch", True, False, WorkerOutcomeStatus.completed.value)
    ]
    assert [event for event in events if event[0] == "active"] == [
        ("active", "call_dispatch", "step-1"),
        ("active", "call_dispatch", "step-2"),
    ]
    assert [event for event in events if event[0] == "done"] == [
        ("done", "call_dispatch", "step-1"),
        ("done", "call_dispatch", "step-2"),
    ]
    assert result.modified_files == [
        "aura/bridge/_shell_pipeline.py",
        "aura/bridge/worker_completion_result.py",
    ]


def test_no_progress_step_stops_campaign_before_next_step():
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    calls = []
    events = []

    def run_step(tool_id, step_req, pending):
        calls.append((tool_id, step_req.goal))
        return WorkerDispatchResult(
            ok=False,
            summary="Worker made no changes.",
            recoverable=True,
            needs_followup=True,
            status=WorkerOutcomeStatus.needs_followup.value,
            extras={},
        )

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        **_session_callbacks(events),
    )

    result = session.run()

    assert result.ok is False
    assert calls == [("call_dispatch", req.steps[0].goal)]
    assert ("active", "call_dispatch", "step-2") not in events
    assert [event for event in events if event[0] == "done"] == []


def test_nonfinal_failed_step_missing_applied_does_not_count_as_progress():
    """A write record with no ``applied`` key must NOT count as file progress."""
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    calls = []
    events = []

    def run_step(tool_id, step_req, pending):
        calls.append((tool_id, step_req.goal))
        return WorkerDispatchResult(
            ok=False,
            summary="Step failed; writes recorded no applied field.",
            recoverable=True,
            needs_followup=True,
            status=WorkerOutcomeStatus.validation_failed.value,
            extras={
                "writes": [{"path": "some/file.py"}],
            },
        )

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        **_session_callbacks(events),
    )

    result = session.run()

    assert result.ok is False
    assert calls == [("call_dispatch", req.steps[0].goal)]
    assert ("active", "call_dispatch", "step-2") not in events


def test_nonfinal_failed_step_applied_false_stays_on_step():
    """A write record with ``applied`` = False must NOT count as file progress."""
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    calls = []
    events = []

    def run_step(tool_id, step_req, pending):
        calls.append((tool_id, step_req.goal))
        return WorkerDispatchResult(
            ok=False,
            summary="Step failed; write was not applied.",
            recoverable=True,
            needs_followup=True,
            status=WorkerOutcomeStatus.validation_failed.value,
            extras={
                "writes": [{"path": "some/file.py", "applied": False}],
            },
        )

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        **_session_callbacks(events),
    )

    result = session.run()

    assert result.ok is False
    assert calls == [("call_dispatch", req.steps[0].goal)]
    assert ("active", "call_dispatch", "step-2") not in events


def test_nonfinal_failed_step_applied_true_advances_to_next_step():
    """A write record with ``applied`` = True and a valid path MUST count as
    file progress and allow the campaign to continue to the next step."""
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    calls = []
    events = []
    results = [
        WorkerDispatchResult(
            ok=False,
            summary="Step 1 created a file but validation failed.",
            recoverable=True,
            needs_followup=True,
            status=WorkerOutcomeStatus.validation_failed.value,
            extras={
                "writes": [{"path": "some/file.py", "applied": True}],
            },
        ),
        WorkerDispatchResult(
            ok=True,
            summary="Step 2 completed successfully.",
            status=WorkerOutcomeStatus.completed.value,
            modified_files=["aura/bridge/worker_completion_result.py"],
        ),
    ]

    def run_step(tool_id, step_req, pending):
        calls.append((tool_id, step_req.goal))
        return results[len(calls) - 1]

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        **_session_callbacks(events),
    )

    result = session.run()

    assert result.ok is True
    assert calls == [
        ("call_dispatch", req.steps[0].goal),
        ("call_dispatch", req.steps[1].goal),
    ]
    assert ("active", "call_dispatch", "step-2") in events


def test_worker_step_message_forbids_campaign_planning():
    req = _request_with_steps()
    message = _format_spec_as_user_message(req)

    assert "Active Dispatch Step" in message
    assert "Do only this step" in message
    assert "Do not plan, decompose, or schedule the whole task" in message


def test_dispatch_session_todo_uses_planner_checklist_rows():
    """The visible TODO rail is driven by planner-authored checklist rows,
    not by step-level objectives. Each checklist item becomes one row,
    and `owning_step_id` controls which step activates/completes which rows.
    """
    req = _request_with_steps()
    req.todo_checklist = [
        DispatchTodoItem(
            id="create-helper",
            description="Create shell pipeline helper module",
            owning_step_id="step-1",
        ),
        DispatchTodoItem(
            id="move-parser",
            description="Move shell parser helper",
            owning_step_id="step-1",
        ),
        DispatchTodoItem(
            id="wire-caller",
            description="Wire helper module into completion result",
            owning_step_id="step-2",
        ),
        DispatchTodoItem(
            id="run-compile",
            description="Run compile validation",
            owning_step_id="step-2",
        ),
    ]
    plan = plan_from_request(req)
    calls = []
    snapshots = []
    controller = DispatchTodoController()

    def save_snapshot(snapshot):
        snapshots.append(snapshot)

    def begin_steps(tool_id, objectives):
        # Use the canonical planner checklist rows (via .to_dict())
        # instead of whatever the dispatch loop passes as objectives.
        save_snapshot(
            controller.begin(tool_id, [item.to_dict() for item in plan.visible_checklist])
        )

    def set_active_step(tool_id, step_id):
        snapshot = controller.activate_step(tool_id, step_id)
        if snapshot is not None:
            save_snapshot(snapshot)

    def mark_step_done(tool_id, step_id):
        snapshot = controller.complete_step(tool_id, step_id)
        if snapshot is not None:
            save_snapshot(snapshot)

    def finish_steps(tool_id):
        snapshot = controller.finish(tool_id)
        if snapshot is not None:
            save_snapshot(snapshot)

    def run_step(tool_id, step_req, pending):
        calls.append((tool_id, step_req.goal))
        return WorkerDispatchResult(
            ok=True,
            summary=f"Completed {step_req.summary}",
            status=WorkerOutcomeStatus.completed.value,
            modified_files=list(step_req.files),
        )

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        begin_steps=begin_steps,
        set_active_step=set_active_step,
        mark_step_done=mark_step_done,
        finish_steps=finish_steps,
    )

    result = session.run()

    assert result.ok is True
    # Four checklist rows, one per DispatchTodoItem, in checklist order.
    expected_descriptions = [
        "Create shell pipeline helper module",
        "Move shell parser helper",
        "Wire helper module into completion result",
        "Run compile validation",
    ]
    assert [row["description"] for row in snapshots[0]] == expected_descriptions
    # Begin: every row pending.
    assert [row["status"] for row in snapshots[0]] == ["pending", "pending", "pending", "pending"]
    # step-1 active — first 2 checklist rows (owned by step-1) light up.
    assert [row["status"] for row in snapshots[1]] == ["active", "active", "pending", "pending"]
    # step-1 done — first 2 rows done; step-2 rows still pending.
    assert [row["status"] for row in snapshots[2]] == ["done", "done", "pending", "pending"]
    # step-2 active — last 2 checklist rows (owned by step-2) light up.
    assert [row["status"] for row in snapshots[3]] == ["done", "done", "active", "active"]
    # step-2 done — the whole rail is complete.
    assert [row["status"] for row in snapshots[4]] == ["done", "done", "done", "done"]
    assert [row["status"] for row in snapshots[-1]] == ["done", "done", "done", "done"]
    assert calls == [
        ("call_dispatch", req.steps[0].goal),
        ("call_dispatch", req.steps[1].goal),
    ]


def test_plan_from_request_expands_acceptance_bullets_into_visible_checklist():
    req = WorkerDispatchRequest(
        goal="Extract WorkerToolEventRouter and wire WorkerEventHandler to it.",
        files=["aura/gui/worker_handler.py", "aura/gui/worker_tool_event_router.py"],
        spec=(
            "Accepted work contract:\n"
            "- Create WorkerToolEventRouter helper module\n"
            "- Move worker tool call start routing\n"
            "- Move worker tool args routing\n"
            "- Move worker tool result routing\n"
            "- Move worker diff decision routing\n"
            "- Move worker terminal output routing\n"
            "- Move agent process start/output/finish routing\n"
            "- Move single-mode terminal routing\n"
            "- Wire WorkerEventHandler to the router\n"
            "- Remove moved method bodies/imports from WorkerEventHandler\n"
            "- Run compile/selfcheck\n"
        ),
        acceptance="The visible TODO rail shows every accepted work item and validation passes.",
        summary="Extract WorkerToolEventRouter.",
        steps=[
            WorkerStepSpec(
                id="step-1",
                title="Create WorkerToolEventRouter helper module",
                goal="Create the router helper module and move routing methods into it.",
                spec="Create the helper module for moved Worker tool/event routing.",
                files=["aura/gui/worker_tool_event_router.py"],
                acceptance="Router module exists with moved routing helpers.",
            ),
            WorkerStepSpec(
                id="step-2",
                title="Wire WorkerToolEventRouter into WorkerEventHandler and remove moved methods",
                goal="Wire WorkerEventHandler to the router and clean up moved code.",
                spec="Wire WorkerEventHandler to the router, remove moved method bodies/imports, and run compile/selfcheck.",
                files=["aura/gui/worker_handler.py"],
                acceptance="WorkerEventHandler delegates to the router and validation passes.",
            ),
        ],
    )

    descriptions = [item.description for item in plan_from_request(req).visible_checklist]

    assert len(descriptions) >= 11
    assert "Create WorkerToolEventRouter helper module" in descriptions
    assert "Move worker tool call start routing" in descriptions
    assert "Move worker tool args routing" in descriptions
    assert "Move worker tool result routing" in descriptions
    assert "Move worker diff decision routing" in descriptions
    assert "Move worker terminal output routing" in descriptions
    assert "Move agent process start/output/finish routing" in descriptions
    assert "Move single-mode terminal routing" in descriptions
    assert "Wire WorkerEventHandler to the router" in descriptions
    assert "Remove moved method bodies/imports from WorkerEventHandler" in descriptions
    assert "Run compile/selfcheck" in descriptions
