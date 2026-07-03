from types import SimpleNamespace

from aura.bridge.dispatch_session import DispatchSession
from aura.bridge.worker_report import _format_spec_as_user_message
from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch_plan import (
    WorkerDispatchPlan,
    WorkerStepSpec,
    plan_from_request,
    request_for_step,
)
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.events import (
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
    EventBus,
)


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


def _session_lifecycle_callbacks(events: list[tuple]) -> dict:
    return {
        "emit_worker_started": lambda tool_id: events.append(("started", tool_id)),
        "emit_worker_finished": lambda tool_id, ok, summary, needs_followup, status: events.append(
            ("finished", tool_id, ok, needs_followup, status)
        ),
    }


def _record_step_events(bus: EventBus, events: list[tuple]) -> None:
    bus.subscribe(
        DISPATCH_STEP_STARTED,
        lambda event: events.append(("active", event.campaign_id, event.step_id)),
    )
    bus.subscribe(
        DISPATCH_STEP_COMPLETED,
        lambda event: events.append(("done", event.campaign_id, event.step_id)),
    )


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
    bus = EventBus()
    _record_step_events(bus, events)
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
        event_bus=bus,
        **_session_lifecycle_callbacks(events),
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
    bus = EventBus()
    _record_step_events(bus, events)

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
        event_bus=bus,
        **_session_lifecycle_callbacks(events),
    )

    result = session.run()

    assert result.ok is False
    assert result.recoverable is False
    assert result.needs_followup is False
    assert result.status == WorkerOutcomeStatus.harness_error.value
    assert calls == [
        ("call_dispatch", req.steps[0].goal),
        ("call_dispatch", req.steps[0].goal),
    ]
    assert ("active", "call_dispatch", "step-2") not in events
    assert [event for event in events if event[0] == "done"] == []
    wp = result.extras["worker_persistence"]
    assert wp["terminal"] is True
    assert wp["reason"] == "worker_step_no_progress"
    assert wp["attempts"] == 2
    assert wp["max_attempts"] == 5
    assert wp["no_progress_threshold"] == 2
    assert isinstance(wp["fingerprint"], str) and len(wp["fingerprint"]) > 0
    assert len(wp["attempt_history"]) == 2
    assert wp["attempt_history"][0]["attempt"] == 1
    assert wp["attempt_history"][1]["attempt"] == 2


def test_first_recoverable_no_progress_retries_with_attempt_context():
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    step_specs: list[str] = []
    tool_ids: list[str] = []
    step_risk_notes: list[list[str]] = []
    bus = EventBus()

    def run_step(tool_id, step_req, pending):
        step_specs.append(step_req.spec)
        tool_ids.append(tool_id)
        step_risk_notes.append(list(step_req.risk_notes))
        if len(step_specs) == 1:
            return WorkerDispatchResult(
                ok=False,
                summary="Worker made no changes.",
                recoverable=True,
                needs_followup=True,
                status=WorkerOutcomeStatus.needs_followup.value,
                extras={"errors": ["No files were changed."]},
            )
        return WorkerDispatchResult(
            ok=True,
            summary="Recovered and completed.",
            status=WorkerOutcomeStatus.completed.value,
            modified_files=["aura/bridge/_shell_pipeline.py"],
        )

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        event_bus=bus,
    )

    result = session.run()

    assert result.ok is True
    assert len(step_specs) == 3
    assert "Worker Persistence Context" not in step_specs[0]
    assert "Worker Persistence Context" in step_specs[1]
    assert "Previous summary: Worker made no changes." in step_specs[1]
    assert all(tid == "call_dispatch" for tid in tool_ids)
    assert "Worker persistence retry" not in " ".join(step_risk_notes[0])
    assert "Worker persistence retry" in " ".join(step_risk_notes[1])
    assert "Worker Persistence Context" not in step_specs[2]


def test_repeated_no_progress_fingerprint_stops_after_retry():
    """Prove the repeated identical no-progress fingerprint stops the step
    after exactly one corrective retry."""
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    calls: list[str] = []

    def run_step(tool_id, step_req, pending):
        calls.append(tool_id)
        return WorkerDispatchResult(
            ok=False,
            summary="No changes.",
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
        event_bus=EventBus(),
    )

    result = session.run()

    assert result.ok is False
    assert len(calls) == 2
    assert all(cid == "call_dispatch" for cid in calls)
    assert result.extras["worker_persistence"]["terminal"] is True
    assert result.extras["worker_persistence"]["reason"] == "worker_step_no_progress"
    assert result.extras["worker_persistence"]["attempts"] == 2
    assert len(result.extras["worker_persistence"]["attempt_history"]) == 2


def test_external_blocker_surfaces_without_persistence_retry():
    req = _request_with_steps()
    plan = WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        steps=list(req.steps),
    )
    calls = []

    def run_step(tool_id, step_req, pending):
        calls.append((tool_id, step_req.goal))
        return WorkerDispatchResult(
            ok=False,
            summary="Permission denied writing required file.",
            recoverable=True,
            needs_followup=True,
            status=WorkerOutcomeStatus.harness_error.value,
            extras={"terminal_environment_blocker": True},
        )

    session = DispatchSession(
        tool_call_id="call_dispatch",
        original_request=req,
        plan=plan,
        run_worker_step=run_step,
        pending=SimpleNamespace(),
        event_bus=EventBus(),
    )

    result = session.run()

    assert result.ok is False
    assert calls == [("call_dispatch", req.steps[0].goal)]
    assert result.extras.get("worker_persistence") is None


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
    bus = EventBus()
    _record_step_events(bus, events)

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
        event_bus=bus,
        **_session_lifecycle_callbacks(events),
    )

    result = session.run()

    assert result.ok is False
    assert calls == [
        ("call_dispatch", req.steps[0].goal),
        ("call_dispatch", req.steps[0].goal),
    ]
    assert ("active", "call_dispatch", "step-2") not in events
    assert result.recoverable is False
    assert result.needs_followup is False
    assert result.status == WorkerOutcomeStatus.harness_error.value
    wp = result.extras["worker_persistence"]
    assert wp["terminal"] is True
    assert wp["reason"] == "worker_step_no_progress"
    assert len(wp["attempt_history"]) == 2


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
    bus = EventBus()
    _record_step_events(bus, events)

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
        event_bus=bus,
        **_session_lifecycle_callbacks(events),
    )

    result = session.run()

    assert result.ok is False
    assert calls == [
        ("call_dispatch", req.steps[0].goal),
        ("call_dispatch", req.steps[0].goal),
    ]
    assert ("active", "call_dispatch", "step-2") not in events
    assert result.recoverable is False
    assert result.needs_followup is False
    assert result.status == WorkerOutcomeStatus.harness_error.value
    wp = result.extras["worker_persistence"]
    assert wp["terminal"] is True
    assert wp["reason"] == "worker_step_no_progress"
    assert len(wp["attempt_history"]) == 2


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
    bus = EventBus()
    _record_step_events(bus, events)
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
        event_bus=bus,
        **_session_lifecycle_callbacks(events),
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


def test_worker_step_request_contains_only_current_step_fields():
    req = _request_with_steps()
    plan = plan_from_request(req)

    step_req = request_for_step(plan, plan.steps[0], req)

    assert step_req.goal == req.steps[0].goal
    assert step_req.summary == req.steps[0].title
    assert step_req.files == req.steps[0].files
    assert step_req.steps == []
    payload = step_req.to_dict()
    assert "steps" not in payload
