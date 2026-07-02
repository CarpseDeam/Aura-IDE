import json
import threading

from aura.client import (
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallStart,
    ToolResult,
    WorkerDispatchRequested,
)
from aura.conversation.dispatch_failure import classify_failed_worker_dispatch
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.dispatch import WorkerDispatchResult
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.verification_progress import VerificationProgressTracker
from aura.conversation.workflow_state import WorkflowStatus
from aura.research.policy import decide_research_policy


def _flat_steps_required_dispatch() -> dict:
    return {
        "goal": "Fix the live Worker Log regression during canonical dispatch.",
        "files": ["aura/gui/worker_handler.py"],
        "spec": (
            "Remove the canonical-dispatch early return guards from "
            "_on_worker_reasoning and _on_worker_content. Delete the guards "
            "only; do not change playground.py."
        ),
        "acceptance": (
            "- _on_worker_reasoning no longer returns early during canonical dispatch\n"
            "- _on_worker_content no longer returns early during canonical dispatch\n"
            "- python -m compileall aura/gui/worker_handler.py passes\n"
            "- python -m aura --selfcheck passes"
        ),
        "summary": "Allow Worker text through during canonical dispatch.",
        "validation_commands": [
            "python -m compileall aura/gui/worker_handler.py",
            "python -m aura --selfcheck",
        ],
    }


def _valid_steps_dispatch() -> dict:
    args = dict(_flat_steps_required_dispatch())
    args["steps"] = [
        {
            "id": "step-1",
            "title": "Worker log dispatch text forwarding",
            "goal": "Allow Worker reasoning and content through during canonical dispatch.",
            "spec": (
                "In aura/gui/worker_handler.py, remove only the canonical-dispatch "
                "early return guards from _on_worker_reasoning and _on_worker_content."
            ),
            "files": ["aura/gui/worker_handler.py"],
            "acceptance": (
                "_on_worker_reasoning and _on_worker_content no longer return early "
                "during canonical dispatch."
            ),
        }
    ]
    return args


def _broad_multi_file_dispatch() -> dict:
    return {
        "goal": "Extract the dispatch checklist projector and wire the bridge.",
        "files": [
            "aura/bridge/dispatch.py",
            "aura/bridge/dispatch_session.py",
            "aura/execution_checklist/controller.py",
        ],
        "spec": "Refactor the dispatch checklist flow across bridge, session, and controller.",
        "acceptance": "The dispatch checklist is projected from lifecycle events and tests pass.",
        "summary": "Refactor dispatch checklist flow.",
    }


def _broad_one_vague_step_dispatch() -> dict:
    args = _broad_multi_file_dispatch()
    args["steps"] = [
        {
            "id": "step-1",
            "title": args["summary"],
            "goal": args["goal"],
            "spec": args["spec"],
            "files": list(args["files"]),
            "acceptance": args["acceptance"],
        }
    ]
    return args


def _broad_valid_steps_no_checklist_dispatch() -> dict:
    args = _broad_multi_file_dispatch()
    args["steps"] = [
        {
            "id": "step-1",
            "title": "Wire bridge checklist projector",
            "goal": "Use the execution checklist controller from the dispatch bridge.",
            "spec": "Update aura/bridge/dispatch.py to source dispatchTodoListUpdated from the execution checklist controller.",
            "files": ["aura/bridge/dispatch.py"],
            "acceptance": "The bridge uses the execution checklist controller.",
        },
        {
            "id": "step-2",
            "title": "Declare checklist lifecycle rows",
            "goal": "Emit checklist declaration facts from DispatchSession without mutating rows.",
            "spec": "Update aura/bridge/dispatch_session.py to declare checklist rows and emit step lifecycle events only.",
            "files": ["aura/bridge/dispatch_session.py"],
            "acceptance": "DispatchSession emits lifecycle events and does not mutate checklist rows.",
        },
    ]
    return args


def _dispatch_tool_call(args: dict, call_id: str = "call_dispatch") -> dict:
    return {
        "id": call_id,
        "function": {
            "name": "dispatch_to_worker",
            "arguments": json.dumps(args),
        },
    }


def _round_runner(tmp_path, history: History | None = None) -> tuple[ToolRoundRunner, History]:
    history = history or History()
    loop_detector = LoopDetector()
    registry = ToolRegistry(tmp_path, mode="planner")
    tool_runner = ToolRunner(
        history,
        tmp_path,
        loop_detector,
        VerificationProgressTracker(),
    )
    return (
        ToolRoundRunner(
            history=history,
            tools=registry,
            tool_runner=tool_runner,
            loop_detector=loop_detector,
            planner_refresh=PlannerRefreshState(),
        ),
        history,
    )


def _planner_state() -> _SendState:
    return _SendState(
        mode="planner",
        research_policy=decide_research_policy("Fix local code in aura/gui/worker_handler.py"),
    )


def test_flat_dispatch_rejection_has_visible_retry_constraint(tmp_path):
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    states = []

    result = runner.handle_dispatch(
        "call_dispatch",
        _flat_steps_required_dispatch(),
        on_event=lambda event: None,
        dispatch_cb=lambda _tool_id, _req: None,
        workflow_state_cb=lambda *items: states.append(items),
    )

    assert result is not None
    assert result.ok is False
    assert result.recoverable is True
    assert result.extras["dispatch_spec_rejected"] is True
    assert result.extras["planner_resolution_needed"] is True
    assert result.extras["internal_planner_handoff"] is True
    assert result.extras["campaign_errors"] == [
        "Broad implementation dispatches must include a decomposed steps campaign."
    ]

    constraint = result.extras["failure_constraint"]
    assert constraint.startswith("CONSTRAINT FOR NEXT DISPATCH ATTEMPT:")
    assert "rejected before Worker start" in constraint
    assert "steps array" in constraint
    assert "id, title, goal, spec, files, and acceptance" in constraint
    assert "Do not call edit/write tools." in constraint
    assert "The Worker was not started" in result.summary
    assert "Planner must retry dispatch_to_worker" in result.summary
    assert states and states[-1][3] == WorkflowStatus.planner_resolving


def test_broad_multi_file_dispatch_without_steps_is_rejected_before_worker_requested(tmp_path):
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    events = []

    result = runner.handle_dispatch(
        "call_dispatch",
        _broad_multi_file_dispatch(),
        on_event=events.append,
        dispatch_cb=lambda _tool_id, _req: (_ for _ in ()).throw(
            AssertionError("invalid dispatch must not start Worker")
        ),
    )

    assert result is not None
    assert result.ok is False
    assert result.recoverable is True
    assert result.extras["campaign_errors"] == [
        "Broad implementation dispatches must include a decomposed steps campaign."
    ]
    assert result.extras["checklist_errors"] == [
        "Broad implementation dispatches must produce a concrete visible execution checklist."
    ]
    assert not any(isinstance(event, WorkerDispatchRequested) for event in events)


def test_broad_dispatch_with_one_vague_giant_step_is_rejected(tmp_path):
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    events = []

    result = runner.handle_dispatch(
        "call_dispatch",
        _broad_one_vague_step_dispatch(),
        on_event=events.append,
        dispatch_cb=lambda _tool_id, _req: (_ for _ in ()).throw(
            AssertionError("invalid dispatch must not start Worker")
        ),
    )

    assert result is not None
    assert result.ok is False
    assert any("split file work" in error for error in result.extras["campaign_errors"])
    assert any("not the full campaign" in error for error in result.extras["campaign_errors"])
    assert not any(isinstance(event, WorkerDispatchRequested) for event in events)


def test_broad_valid_steps_without_checklist_gets_step_fallback_and_starts_worker(tmp_path):
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    events = []
    dispatches = []

    result = runner.handle_dispatch(
        "call_dispatch",
        _broad_valid_steps_no_checklist_dispatch(),
        on_event=events.append,
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
    )

    assert result is not None
    assert result.ok is True
    assert dispatches and dispatches[0][0] == "call_dispatch"
    req = dispatches[0][1]
    assert [item.id for item in req.todo_checklist] == ["step-1", "step-2"]
    assert [item.owning_step_id for item in req.todo_checklist] == ["step-1", "step-2"]
    assert any(isinstance(event, WorkerDispatchRequested) for event in events)


def test_failed_dispatch_classification_preserves_campaign_constraint(tmp_path):
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    result = runner.handle_dispatch(
        "call_dispatch",
        _flat_steps_required_dispatch(),
        on_event=lambda event: None,
        dispatch_cb=lambda _tool_id, _req: None,
    )
    assert result is not None

    action = classify_failed_worker_dispatch(
        args=_flat_steps_required_dispatch(),
        result=result,
        failures={},
        failed_attempts=0,
    )

    assert action["blocker_reason"] == ""
    assert action["failure_constraint"] == result.extras["failure_constraint"]


def test_missing_steps_dispatch_round_appends_tool_result_and_internal_constraint(tmp_path):
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []
    tool_call = _dispatch_tool_call(_flat_steps_required_dispatch())
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [tool_call],
        }
    )

    outcome = runner.run(
        tool_calls=[tool_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: (_ for _ in ()).throw(
            AssertionError("invalid dispatch must not start Worker")
        ),
        cleanup_cancelled=lambda on_event: None,
    )

    assert outcome.action == "continue"
    assert not any(isinstance(event, Done) for event in events)
    assert not any(isinstance(event, WorkerDispatchRequested) for event in events)

    tool_results = [msg for msg in history.messages if msg.get("role") == "tool"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == "call_dispatch"
    payload = json.loads(tool_results[0]["content"])
    assert payload["ok"] is False
    assert payload["extras"]["dispatch_spec_rejected"] is True
    assert payload["extras"]["internal_planner_handoff"] is True
    assert payload["extras"]["user_visible_blocker"] is False

    internal_constraints = [
        msg for msg in history.messages if msg.get("aura_internal") is True
    ]
    assert len(internal_constraints) == 1
    assert internal_constraints[0]["content"] == payload["extras"]["failure_constraint"]
    assert history.messages.index(tool_results[0]) < history.messages.index(
        internal_constraints[0]
    )

    event_results = [event for event in events if isinstance(event, ToolResult)]
    assert len(event_results) == 1
    assert event_results[0].ok is False
    assert event_results[0].extras["internal_planner_handoff"] is True


def test_internal_dispatch_handoff_does_not_use_blocker_done_path(tmp_path):
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    tool_call = _dispatch_tool_call(_flat_steps_required_dispatch())
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [tool_call],
        }
    )

    def fail_blocker(*args, **kwargs):
        raise AssertionError("internal handoff must not use dispatch blocker path")

    runner._append_dispatch_blocker_message = fail_blocker

    outcome = runner.run(
        tool_calls=[tool_call],
        state=state,
        on_event=lambda event: None,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: (_ for _ in ()).throw(
            AssertionError("invalid dispatch must not start Worker")
        ),
        cleanup_cancelled=lambda on_event: None,
    )

    assert outcome.action == "continue"


def test_repeated_dispatch_shape_rejection_remains_terminal_blocker(tmp_path):
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []

    first_call = _dispatch_tool_call(_flat_steps_required_dispatch(), "call_dispatch_1")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [first_call],
        }
    )
    first_outcome = runner.run(
        tool_calls=[first_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: None,
        cleanup_cancelled=lambda on_event: None,
    )
    assert first_outcome.action == "continue"

    second_call = _dispatch_tool_call(_flat_steps_required_dispatch(), "call_dispatch_2")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [second_call],
        }
    )
    second_outcome = runner.run(
        tool_calls=[second_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: None,
        cleanup_cancelled=lambda on_event: None,
    )

    assert second_outcome.action == "return"
    assert any(
        isinstance(event, Done) and event.finish_reason == "stop"
        for event in events
    )


def test_valid_steps_dispatch_after_internal_rejection_starts_worker(tmp_path):
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []

    invalid_call = _dispatch_tool_call(_flat_steps_required_dispatch(), "call_bad")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [invalid_call],
        }
    )
    invalid_outcome = runner.run(
        tool_calls=[invalid_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: None,
        cleanup_cancelled=lambda on_event: None,
    )
    assert invalid_outcome.action == "continue"

    dispatches = []
    valid_call = _dispatch_tool_call(_valid_steps_dispatch(), "call_good")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [valid_call],
        }
    )
    valid_outcome = runner.run(
        tool_calls=[valid_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        cleanup_cancelled=lambda on_event: None,
    )

    assert valid_outcome.action == "return"
    assert dispatches and dispatches[0][0] == "call_good"
    assert any(
        isinstance(event, WorkerDispatchRequested)
        and event.tool_call_id == "call_good"
        for event in events
    )


def test_planner_edit_tool_misuse_gets_dispatch_correction(tmp_path):
    registry = ToolRegistry(tmp_path, mode="planner")

    result = registry.execute("edit_file", {}, approval_cb=lambda request: None)

    assert result.ok is False
    assert result.payload["planner_tool_unavailable"] is True
    assert result.payload["suggested_next_tool"] == "dispatch_to_worker"
    assert result.extras["internal_planner_handoff"] is True
    assert "Planner never edits files directly" in result.payload["error"]
    assert "Do not call edit/write tools" in result.extras["failure_constraint"]
    assert "dispatch_to_worker" in result.extras["failure_constraint"]


def test_planner_registered_write_tool_is_also_blocked_with_correction(tmp_path):
    registry = ToolRegistry(tmp_path, mode="planner")

    result = registry.execute(
        "write_file",
        {"path": "example.py", "content": "print('nope')\n"},
        approval_cb=lambda request: None,
    )

    assert result.ok is False
    payload = json.loads(result.to_tool_message_content())
    assert payload["planner_tool_unavailable"] is True
    assert payload["suggested_next_tool"] == "dispatch_to_worker"
    assert "write_file is not available in Planner mode" in payload["error"]


# ---------------------------------------------------------------------------
# Silent preflight: internal dispatch repair must not leak visible chatter
# ---------------------------------------------------------------------------


def test_internal_handoff_returns_enter_silent_preflight(tmp_path):
    """ToolRoundOutcome for a recoverable dispatch rejection must signal
    that the next Planner turn should run in silent preflight mode."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    tool_call = _dispatch_tool_call(_flat_steps_required_dispatch())
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [tool_call],
        }
    )

    outcome = runner.run(
        tool_calls=[tool_call],
        state=state,
        on_event=lambda event: None,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: None,
        cleanup_cancelled=lambda on_event: None,
    )

    assert outcome.action == "continue"
    assert outcome.enter_silent_preflight is True


def test_silent_preflight_suppresses_all_visible_events():
    """When silent_preflight is active, ALL stream events (ContentDelta,
    ReasoningDelta, ToolCallStart, ToolCallArgsDelta, ToolCallEnd, Usage,
    Done) must be suppressed from reaching the UI -- only the Done
    full_message is captured for the caller."""
    from aura.client import ToolCallArgsDelta, ToolCallEnd, Usage

    def _run_stream(events, *, silent_preflight):
        forwarded = []
        full_message = None
        for ev in events:
            if silent_preflight:
                if isinstance(ev, (ContentDelta, ReasoningDelta,
                                   ToolCallStart, ToolCallArgsDelta,
                                   ToolCallEnd, Usage)):
                    continue
                if isinstance(ev, Done):
                    full_message = ev.full_message
                    continue
            forwarded.append(ev)
            if isinstance(ev, Done):
                full_message = ev.full_message
        return forwarded, full_message

    events = [
        ReasoningDelta(text="I see the dispatch was rejected..."),
        ContentDelta(text="The dispatch was rejected because step 6 has an empty files array."),
        ToolCallStart(index=0, id="call_1", name="read_file"),
        ToolCallArgsDelta(index=0, args_chunk='{"path":'),
        ToolCallEnd(index=0),
        Usage(prompt_tokens=100, completion_tokens=50, cache_hit_tokens=0, cache_miss_tokens=100),
        Done(finish_reason="tool_calls", full_message={"role": "assistant", "content": "", "tool_calls": []}),
    ]

    forwarded_off, _ = _run_stream(events, silent_preflight=False)
    assert any(isinstance(e, ReasoningDelta) for e in forwarded_off)
    assert any(isinstance(e, ContentDelta) for e in forwarded_off)
    assert any(isinstance(e, ToolCallStart) for e in forwarded_off)
    assert any(isinstance(e, ToolCallArgsDelta) for e in forwarded_off)
    assert any(isinstance(e, ToolCallEnd) for e in forwarded_off)
    assert any(isinstance(e, Usage) for e in forwarded_off)
    assert any(isinstance(e, Done) for e in forwarded_off)

    forwarded_on, fm = _run_stream(events, silent_preflight=True)
    # No events of any type reach the UI during silent preflight
    assert not any(isinstance(e, (ReasoningDelta, ContentDelta,
                                  ToolCallStart, ToolCallArgsDelta,
                                  ToolCallEnd, Usage, Done))
                   for e in forwarded_on)
    assert len(forwarded_on) == 0
    # full_message must still be captured from suppressed Done
    assert fm is not None
    assert fm["role"] == "assistant"


def test_worker_dispatch_requested_not_emitted_during_preflight_rejection(tmp_path):
    """WorkerDispatchRequested must NOT fire for a rejected preflight dispatch;
    it must fire only when a valid campaign passes dispatch validation."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []

    # Round 1: invalid dispatch — internal handoff, no WorkerDispatchRequested
    invalid_call = _dispatch_tool_call(_flat_steps_required_dispatch(), "call_bad")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [invalid_call],
        }
    )
    invalid_outcome = runner.run(
        tool_calls=[invalid_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: None,
        cleanup_cancelled=lambda on_event: None,
    )
    assert invalid_outcome.action == "continue"
    assert invalid_outcome.enter_silent_preflight is True
    assert not any(isinstance(e, WorkerDispatchRequested) for e in events)

    # Round 2: valid dispatch — WorkerDispatchRequested IS emitted
    dispatches = []
    valid_call = _dispatch_tool_call(_valid_steps_dispatch(), "call_good")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [valid_call],
        }
    )
    valid_outcome = runner.run(
        tool_calls=[valid_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        cleanup_cancelled=lambda on_event: None,
    )

    assert valid_outcome.action == "return"
    assert dispatches and dispatches[0][0] == "call_good"
    assert any(
        isinstance(e, WorkerDispatchRequested) and e.tool_call_id == "call_good"
        for e in events
    )


def test_worker_log_todo_not_seeded_for_rejected_preflight_dispatch(tmp_path):
    """A rejected preflight dispatch must set workflow status to
    planner_resolving, not worker_starting. The TODO rail is NOT seeded."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    states = []

    result = runner.handle_dispatch(
        "call_dispatch",
        _flat_steps_required_dispatch(),
        on_event=lambda event: None,
        dispatch_cb=lambda _tool_id, _req: None,
        workflow_state_cb=lambda *items: states.append(items),
    )

    assert result is not None
    assert result.ok is False
    assert states and states[-1][3] == WorkflowStatus.planner_resolving
    assert not any(s[3] == WorkflowStatus.dispatched for s in states)


def test_terminal_blocker_still_surfaces_during_preflight(tmp_path):
    """After silent preflight is active, a repeated dispatch shape rejection
    must still produce a terminal blocker with a Done(stop) event."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []

    # First rejection: internal handoff, enters silent preflight
    first_call = _dispatch_tool_call(_flat_steps_required_dispatch(), "call_dispatch_1")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [first_call],
        }
    )
    first_outcome = runner.run(
        tool_calls=[first_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: None,
        cleanup_cancelled=lambda on_event: None,
    )
    assert first_outcome.action == "continue"
    assert first_outcome.enter_silent_preflight is True

    # Second rejection: same signature, becomes terminal blocker
    second_call = _dispatch_tool_call(_flat_steps_required_dispatch(), "call_dispatch_2")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [second_call],
        }
    )
    second_outcome = runner.run(
        tool_calls=[second_call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=lambda _tool_id, _req: None,
        cleanup_cancelled=lambda on_event: None,
    )

    # Terminal blocker must still surface
    assert second_outcome.action == "return"
    assert any(
        isinstance(event, Done) and event.finish_reason == "stop"
        for event in events
    )
