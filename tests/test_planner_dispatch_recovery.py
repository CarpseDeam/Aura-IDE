"""Tests for Planner dispatch recovery flows.

Replaces the old campaign-step validation tests with tests for the new
WorkArtifact compatibility artifact behavior.  Flat dispatches (without a
``work_artifact`` payload) now proceed as one-item compatibility artifacts
rather than being rejected.

Key invariants tested here:
- Flat dispatch creates one-item compatibility artifact and starts Worker
- WorkArtifact dispatch creates multi-item artifact, starts Worker for item 1
- Second dispatch in same Planner turn is still rejected (chained rejection)
- Planner edit/write tools are still blocked with correction
- Compatibility artifact handles broad multi-file dispatches
- Dispatching a work_artifact produces correct item-scoped request
"""

import json
import logging
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


# ── helpers ──────────────────────────────────────────────────────────────────


def _flat_dispatch() -> dict:
    """A flat dispatch_to_worker call without ``work_artifact``.

    In the old architecture this was rejected as "missing steps / campaign".
    Now it creates a one-item compatibility artifact and starts the Worker.
    """
    return {
        "goal": "Fix the live Worker Log regression during canonical dispatch.",
        "files": ["aura/gui/worker_handler.py"],
        "spec": (
            "Remove the canonical-dispatch early return guards from "
            "_on_worker_reasoning and _on_worker_content."
        ),
        "acceptance": (
            "- _on_worker_reasoning no longer returns early\n"
            "- _on_worker_content no longer returns early\n"
            "- python -m compileall aura/gui/worker_handler.py passes"
        ),
        "summary": "Allow Worker text through during canonical dispatch.",
    }


def _work_artifact_dispatch() -> dict:
    """A dispatch_to_worker call with a ``work_artifact`` (multi-item)."""
    return {
        "goal": "Refactor dispatch lifecycle flow.",
        "files": [
            "aura/bridge/dispatch.py",
            "aura/bridge/dispatch_session.py",
            "aura/bridge/worker_activity.py",
        ],
        "spec": "Refactor the dispatch lifecycle flow across bridge, session, and activity projection.",
        "acceptance": "The dispatch lifecycle is projected from events and tests pass.",
        "summary": "Refactor dispatch lifecycle flow.",
        "work_artifact": {
            "goal": "Refactor dispatch lifecycle flow.",
            "constraints": ["No new dependencies"],
            "allowed_files": ["aura/bridge/"],
            "items": [
                {
                    "id": "item-1",
                    "title": "Wire bridge lifecycle projector",
                    "intent": "Use the activity controller from the dispatch bridge.",
                    "target_files": ["aura/bridge/dispatch.py"],
                    "acceptance": "The bridge uses the activity controller.",
                },
                {
                    "id": "item-2",
                    "title": "Emit dispatch lifecycle events",
                    "intent": "Emit work artifact lifecycle events.",
                    "target_files": ["aura/work_artifact/"],
                    "acceptance": "Work artifact emits lifecycle events.",
                },
            ],
        },
    }


def _broad_multi_file_dispatch() -> dict:
    return {
        "goal": "Extract the dispatch lifecycle projector and wire the bridge.",
        "files": [
            "aura/bridge/dispatch.py",
            "aura/bridge/dispatch_session.py",
            "aura/bridge/worker_activity.py",
        ],
        "spec": "Refactor the dispatch lifecycle flow.",
        "acceptance": "The dispatch lifecycle is projected from events and tests pass.",
        "summary": "Refactor dispatch lifecycle flow.",
    }


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


def _dispatch_ok_cb(tool_id: str, req):
    return WorkerDispatchResult(ok=True, summary="Worker completed.")


# ── Flat dispatch (compatibility artifact) tests ─────────────────────────────


def test_flat_dispatch_proceeds_as_compatibility_artifact(tmp_path):
    """A flat dispatch (no work_artifact) creates a compat artifact and runs Worker."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    dispatches = []
    result = runner.handle_dispatch(
        "call_dispatch",
        _flat_dispatch(),
        on_event=lambda event: None,
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
    )
    assert result is not None
    assert result.ok is True
    assert len(dispatches) == 1
    _, dispatched_req = dispatches[0]
    assert dispatched_req.artifact_id == "call_dispatch"
    assert dispatched_req.artifact_item_id == "item-1"


def test_flat_dispatch_emits_worker_dispatch_requested(tmp_path):
    """A flat dispatch emits WorkerDispatchRequested (no longer silently rejected)."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    events = []
    result = runner.handle_dispatch(
        "call_dispatch",
        _flat_dispatch(),
        on_event=events.append,
        dispatch_cb=lambda tool_id, req: WorkerDispatchResult(ok=True, summary="OK"),
    )
    assert result is not None
    assert result.ok is True
    assert any(isinstance(e, WorkerDispatchRequested) for e in events)


def test_broad_multi_file_flat_dispatch_proceeds(tmp_path):
    """Broad multi-file flat dispatch still works as compatibility artifact."""
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
        _broad_multi_file_dispatch(),
        on_event=events.append,
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
    )
    assert result is not None
    assert result.ok is True
    assert len(dispatches) == 1
    assert any(isinstance(e, WorkerDispatchRequested) for e in events)


def test_flat_dispatch_compat_artifact_has_item_one(tmp_path):
    """Compatibility artifact has a single item with id 'item-1'."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    dispatches = []
    runner.handle_dispatch(
        "call_dispatch",
        _flat_dispatch(),
        on_event=lambda event: None,
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="OK"),
        )[1],
    )
    assert len(dispatches) == 1
    _, req = dispatches[0]
    assert req.artifact_item_id == "item-1"
    assert req.artifact_id == "call_dispatch"


# ── WorkArtifact dispatch tests ──────────────────────────────────────────────


def test_work_artifact_dispatch_starts_worker_for_item_one(tmp_path):
    """Multi-item work_artifact starts Worker for item 1 only."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    dispatches = []
    result = runner.handle_dispatch(
        "call_dispatch",
        _work_artifact_dispatch(),
        on_event=lambda event: None,
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
    )
    assert result is not None
    assert result.ok is True
    assert len(dispatches) == 1
    _, req = dispatches[0]
    assert "Wire bridge lifecycle projector" in req.summary


def test_work_artifact_multi_item_emit_worker_dispatch_requested(tmp_path):
    """WorkArtifact dispatch emits WorkerDispatchRequested for item 1."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    events = []
    result = runner.handle_dispatch(
        "call_dispatch",
        _work_artifact_dispatch(),
        on_event=events.append,
        dispatch_cb=lambda tool_id, req: WorkerDispatchResult(ok=True, summary="OK"),
    )
    assert result is not None
    assert result.ok is True
    assert any(isinstance(e, WorkerDispatchRequested) for e in events)


# ── Chained dispatch rejection ───────────────────────────────────────────────


def test_second_dispatch_in_same_planner_turn_is_recoverable_error_not_worker(
    tmp_path,
    caplog,
):
    """Second dispatch_to_worker in same Planner turn is still rejected as chained."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []
    dispatches = []
    caplog.set_level(logging.INFO, logger="aura.conversation.tool_runner")

    first_call = _dispatch_tool_call(_work_artifact_dispatch(), "call_dispatch_1")
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
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        cleanup_cancelled=lambda on_event: None,
    )

    assert first_outcome.action == "return"
    assert state.planner_visible_dispatch_tool_call_id == "call_dispatch_1"

    second_call = _dispatch_tool_call(_work_artifact_dispatch(), "call_dispatch_2")
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
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        cleanup_cancelled=lambda on_event: None,
    )

    assert second_outcome.action == "continue"
    assert [tool_id for tool_id, _req in dispatches] == ["call_dispatch_1"]
    worker_events = [event for event in events if isinstance(event, WorkerDispatchRequested)]
    assert [event.tool_call_id for event in worker_events] == ["call_dispatch_1"]

    tool_results = [msg for msg in history.messages if msg.get("role") == "tool"]
    second_payload = json.loads(tool_results[-1]["content"])
    assert second_payload["ok"] is False
    assert second_payload["recoverable"] is True
    assert second_payload["extras"]["planner_dispatch_chain_rejected"] is True
    assert second_payload["extras"]["previous_dispatch_tool_call_id"] == "call_dispatch_1"
    assert "already dispatched a Worker" in second_payload["summary"]
    assert "work_artifact" in second_payload["extras"]["failure_constraint"]

    dispatch_log_messages = [
        record.getMessage()
        for record in caplog.records
        if "planner_dispatch_entry" in record.getMessage()
    ]
    assert any(
        "tool_call_id=call_dispatch_1" in message
        and "first_dispatch_in_turn=True" in message
        for message in dispatch_log_messages
    )
    assert any(
        "tool_call_id=call_dispatch_2" in message
        and "chained_later_dispatch=True" in message
        and "previous_dispatch_tool_call_id=call_dispatch_1" in message
        for message in dispatch_log_messages
    )


def test_valid_dispatch_after_chained_rejection_starts_worker(tmp_path):
    """A valid dispatch in a new turn after a chained rejection still starts Worker."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []

    first_call = _dispatch_tool_call(_work_artifact_dispatch(), "call_good")
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
        dispatch_cb=_dispatch_ok_cb,
        cleanup_cancelled=lambda on_event: None,
    )
    assert first_outcome.action == "return"

    state.planner_visible_dispatch_tool_call_id = ""

    dispatches = []
    valid_call = _dispatch_tool_call(_work_artifact_dispatch(), "call_good_2")
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
    assert dispatches and dispatches[0][0] == "call_good_2"
    assert any(
        isinstance(event, WorkerDispatchRequested)
        and event.tool_call_id == "call_good_2"
        for event in events
    )


# ── Planner tool blocking ────────────────────────────────────────────────────


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


# ── Silent preflight unit test ───────────────────────────────────────────────


def test_silent_preflight_suppresses_all_visible_events():
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
        ReasoningDelta(text="reasoning..."),
        ContentDelta(text="content..."),
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
    assert not any(isinstance(e, (ReasoningDelta, ContentDelta,
                                  ToolCallStart, ToolCallArgsDelta,
                                  ToolCallEnd, Usage, Done))
                   for e in forwarded_on)
    assert len(forwarded_on) == 0
    assert fm is not None
    assert fm["role"] == "assistant"


# ── Dispatch result metadata ─────────────────────────────────────────────────


def test_failed_dispatch_classification_preserves_constraint(tmp_path):
    """A Worker dispatch failure preserves its failure constraint."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )
    # Simulate a failed dispatch to test classification
    result = WorkerDispatchResult(
        ok=False,
        summary="Worker encountered an error",
        extras={"internal_planner_handoff": True, "failure_constraint": "CONSTRAINT: retry with valid spec"},
    )

    action = classify_failed_worker_dispatch(
        result=result,
    )
    assert action["blocker_reason"] == ""  # internal_planner_handoff clears blocker_reason
    assert "retry with valid spec" in action["failure_constraint"]


def test_recoverable_quality_continuation_dispatch_is_not_blocker():
    result = WorkerDispatchResult(
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

    action = classify_failed_worker_dispatch(result=result)

    assert action["blocker_reason"] == ""
    assert action["failure_constraint"] == ""


# ── ToolRoundRunner dispatch tests ───────────────────────────────────────────


def test_dispatch_round_with_flat_args_proceeds(tmp_path):
    """A flat dispatch through ToolRoundRunner proceeds (no internal rejection)."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []
    dispatches = []
    tool_call = _dispatch_tool_call(_flat_dispatch())
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
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="Worker completed."),
        )[1],
        cleanup_cancelled=lambda on_event: None,
    )

    assert outcome.action == "return"
    assert len(dispatches) == 1
    assert any(isinstance(event, WorkerDispatchRequested) for event in events)

    tool_results = [msg for msg in history.messages if msg.get("role") == "tool"]
    assert len(tool_results) == 1
    payload = json.loads(tool_results[0]["content"])
    assert payload["ok"] is True


def test_chained_dispatch_in_round_rejected_without_worker(tmp_path):
    """A second dispatch call in one Planner turn is rejected without running Worker."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []
    dispatches = []

    first_call = _dispatch_tool_call(_flat_dispatch(), "call_dispatch_1")
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
        dispatch_cb=lambda tool_id, req: (
            dispatches.append((tool_id, req)),
            WorkerDispatchResult(ok=True, summary="OK"),
        )[1],
        cleanup_cancelled=lambda on_event: None,
    )
    assert first_outcome.action == "return"

    second_call = _dispatch_tool_call(_flat_dispatch(), "call_dispatch_2")
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
        dispatch_cb=lambda _tool_id, _req: (
            (_ for _ in ()).throw(AssertionError("chained dispatch must not start Worker"))
        ),
        cleanup_cancelled=lambda on_event: None,
    )

    assert second_outcome.action == "continue"
    assert len(dispatches) == 1
    worker_events = [e for e in events if isinstance(e, WorkerDispatchRequested)]
    assert len(worker_events) == 1

    tool_results = [msg for msg in history.messages if msg.get("role") == "tool"]
    second_payload = json.loads(tool_results[-1]["content"])
    assert second_payload["ok"] is False
    assert second_payload["extras"]["planner_dispatch_chain_rejected"] is True
    assert second_payload["extras"]["previous_dispatch_tool_call_id"] == "call_dispatch_1"


def test_work_artifact_dispatch_emits_worker_dispatch_requested_in_round(tmp_path):
    """WorkArtifact dispatch through ToolRoundRunner emits WorkerDispatchRequested."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []

    call = _dispatch_tool_call(_work_artifact_dispatch(), "call_good")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [call],
        }
    )
    outcome = runner.run(
        tool_calls=[call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=_dispatch_ok_cb,
        cleanup_cancelled=lambda on_event: None,
    )

    assert outcome.action == "return"
    assert any(
        isinstance(e, WorkerDispatchRequested) and e.tool_call_id == "call_good"
        for e in events
    )


def test_flat_dispatch_in_round_proceeds_with_worker_dispatch_requested(tmp_path):
    """Flat dispatch through ToolRoundRunner also emits WorkerDispatchRequested."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    events = []

    call = _dispatch_tool_call(_flat_dispatch(), "call_good")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [call],
        }
    )
    outcome = runner.run(
        tool_calls=[call],
        state=state,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=_dispatch_ok_cb,
        cleanup_cancelled=lambda on_event: None,
    )

    assert outcome.action == "return"
    assert any(
        isinstance(e, WorkerDispatchRequested) and e.tool_call_id == "call_good"
        for e in events
    )


def test_internal_handoff_does_not_use_blocker_done_path(tmp_path):
    """Internal dispatch handoff flows don't use the blocker done path."""
    runner, history = _round_runner(tmp_path)
    state = _planner_state()
    tool_call = _dispatch_tool_call(_flat_dispatch())
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
        dispatch_cb=lambda _tool_id, _req: WorkerDispatchResult(ok=True, summary="OK"),
        cleanup_cancelled=lambda on_event: None,
    )

    assert outcome.action == "return"


def test_worker_dispatch_requested_emitted_for_both_dispatch_types(tmp_path):
    """WorkerDispatchRequested fires for both flat and work_artifact dispatches."""
    runner = ToolRunner(
        History(),
        tmp_path,
        LoopDetector(),
        VerificationProgressTracker(),
    )

    flat_events = []
    runner.handle_dispatch(
        "call_flat",
        _flat_dispatch(),
        on_event=flat_events.append,
        dispatch_cb=_dispatch_ok_cb,
    )
    assert any(isinstance(e, WorkerDispatchRequested) for e in flat_events)

    wa_events = []
    runner.handle_dispatch(
        "call_wa",
        _work_artifact_dispatch(),
        on_event=wa_events.append,
        dispatch_cb=_dispatch_ok_cb,
    )
    assert any(isinstance(e, WorkerDispatchRequested) for e in wa_events)
