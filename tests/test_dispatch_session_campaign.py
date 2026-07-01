from types import SimpleNamespace

from aura.bridge.dispatch_session import DispatchSession
from aura.bridge.worker_report import _format_spec_as_user_message
from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch_plan import WorkerDispatchPlan, WorkerStepSpec
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
