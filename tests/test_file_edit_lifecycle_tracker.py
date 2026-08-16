"""Unit tests for FileEditLifecycleTracker: proposed -> awaiting_approval ->
applied | rejected | failed.

These are pure-Python: no Qt, no ToolRegistry, no filesystem. They exercise
the tracker directly against fake ``ApprovalRequest``/``ApprovalDecision``/
``ToolExecResult`` objects to prove the lifecycle-truth invariants:
"applied" is only ever emitted from an actual applied=True result, a
rejection never gets a second "failed" emission, and two independently
constructed trackers never share state (the concurrency-safety property that
replaces the old shared ``_last_event`` correlation).
"""
from __future__ import annotations

from aura.client import FileEditLifecycle
from aura.conversation.tools._types import (
    ApprovalDecision,
    ApprovalFileChange,
    ApprovalRequest,
    ToolExecResult,
)
from aura.conversation.tools.file_edit_lifecycle import (
    FILE_EDIT_LIFECYCLE_TOOLS,
    FileEditLifecycleTracker,
)


def _collector() -> tuple[list[FileEditLifecycle], callable]:
    events: list[FileEditLifecycle] = []
    return events, events.append


def _write_req(rel_path: str = "a.py", is_new_file: bool = False) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="write_file",
        rel_path=rel_path,
        old_content="old",
        new_content="new",
        is_new_file=is_new_file,
    )


def test_write_tools_are_the_only_tracked_lifecycle_tools() -> None:
    assert FILE_EDIT_LIFECYCLE_TOOLS == {"write_file", "patch_file", "delete_file"}


def test_approved_write_emits_proposed_then_awaiting_then_applied_in_order() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="write_file", on_event=on_event
    )
    wrapped = tracker.wrap_approval_cb(lambda req: ApprovalDecision(action="approve"))

    decision = wrapped(_write_req())
    assert decision.action == "approve"

    tracker.emit_outcome(
        ToolExecResult(ok=True, payload={"ok": True, "applied": True, "rel_path": "a.py"})
    )

    phases = [e.phase for e in events]
    assert phases == ["proposed", "awaiting_approval", "applied"]
    assert all(e.tool_call_id == "call-1" for e in events)
    assert all(e.tool_name == "write_file" for e in events)
    applied = events[-1]
    assert [c.path for c in applied.changes] == ["a.py"]
    assert applied.changes[0].action == "modify"


def test_new_file_action_is_create() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="write_file", on_event=on_event
    )
    wrapped = tracker.wrap_approval_cb(lambda req: ApprovalDecision(action="approve"))
    wrapped(_write_req(is_new_file=True))
    assert events[0].changes[0].action == "create"


def test_delete_file_action_is_delete() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="delete_file", on_event=on_event
    )
    req = ApprovalRequest(
        tool_name="delete_file",
        rel_path="gone.py",
        old_content="x = 1\n",
        new_content="",
        is_new_file=False,
    )
    wrapped = tracker.wrap_approval_cb(lambda r: ApprovalDecision(action="approve"))
    wrapped(req)
    assert events[0].changes[0].action == "delete"


def test_rejected_write_emits_no_applied_phase() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="write_file", on_event=on_event
    )
    wrapped = tracker.wrap_approval_cb(lambda req: ApprovalDecision(action="reject"))

    wrapped(_write_req())
    # A write handler never proceeds to execute() after a rejection, so no
    # ToolExecResult exists to feed emit_outcome -- but even if something
    # tried, it must be a no-op after the terminal "rejected" phase.
    tracker.emit_outcome(
        ToolExecResult(ok=True, payload={"ok": True, "applied": True, "rel_path": "a.py"})
    )

    phases = [e.phase for e in events]
    assert phases == ["proposed", "awaiting_approval", "rejected"]
    assert "applied" not in phases
    assert "failed" not in phases


def test_stale_approval_after_approve_becomes_failed_not_applied() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="write_file", on_event=on_event
    )
    wrapped = tracker.wrap_approval_cb(lambda req: ApprovalDecision(action="approve"))
    wrapped(_write_req())

    tracker.emit_outcome(
        ToolExecResult(
            ok=False,
            payload={
                "ok": False,
                "applied": False,
                "failure_class": "stale_approval",
                "path": "a.py",
            },
        )
    )

    phases = [e.phase for e in events]
    assert phases == ["proposed", "awaiting_approval", "failed"]
    assert events[-1].reason == "stale_approval"


def test_rolled_back_patch_transaction_never_emits_applied() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="patch_file", on_event=on_event
    )
    req = ApprovalRequest(
        tool_name="patch_file",
        rel_path="a.py",
        old_content="old-a",
        new_content="new-a",
        is_new_file=False,
        changes=(
            ApprovalFileChange("a.py", "old-a", "new-a", False),
            ApprovalFileChange("b.py", "old-b", "new-b", False),
        ),
    )
    wrapped = tracker.wrap_approval_cb(lambda r: ApprovalDecision(action="approve"))
    wrapped(req)

    tracker.emit_outcome(
        ToolExecResult(
            ok=False,
            payload={
                "ok": False,
                "applied": False,
                "rolled_back": True,
                "failure_class": "patch_transaction_commit_failed",
                "write_outcome": "not_applied_transaction_rolled_back",
                "path": "b.py",
            },
        )
    )

    phases = [e.phase for e in events]
    assert phases == ["proposed", "awaiting_approval", "failed"]
    failed = events[-1]
    assert {c.path for c in failed.changes} == {"a.py", "b.py"}


def test_partial_applied_transaction_rollback_failure_never_emits_applied() -> None:
    """The 'applied: partial' hazard case must still surface as failed."""
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="patch_file", on_event=on_event
    )
    req = ApprovalRequest(
        tool_name="patch_file",
        rel_path="a.py",
        old_content="old-a",
        new_content="new-a",
        is_new_file=False,
        changes=(ApprovalFileChange("a.py", "old-a", "new-a", False),),
    )
    wrapped = tracker.wrap_approval_cb(lambda r: ApprovalDecision(action="approve"))
    wrapped(req)

    tracker.emit_outcome(
        ToolExecResult(
            ok=False,
            payload={
                "ok": False,
                "applied": "partial",
                "rolled_back": False,
                "failure_class": "transaction_rollback_failed",
                "workspace_state": "potentially_partial",
                "path": "a.py",
            },
        )
    )

    phases = [e.phase for e in events]
    assert phases == ["proposed", "awaiting_approval", "failed"]


def test_multi_file_patch_gets_stable_distinct_change_identities() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-7", tool_name="patch_file", on_event=on_event
    )
    req = ApprovalRequest(
        tool_name="patch_file",
        rel_path="a.py",
        old_content="old-a",
        new_content="new-a",
        is_new_file=False,
        changes=(
            ApprovalFileChange("a.py", "old-a", "new-a", False),
            ApprovalFileChange("b.py", "old-b", "new-b", False),
        ),
    )
    wrapped = tracker.wrap_approval_cb(lambda r: ApprovalDecision(action="approve"))
    wrapped(req)

    proposed = events[0]
    change_ids = [c.change_id for c in proposed.changes]
    assert len(change_ids) == len(set(change_ids)) == 2
    assert all(cid.startswith("call-7:") for cid in change_ids)

    tracker.emit_outcome(
        ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "applied": True,
                "file_count": 2,
                "files": [
                    {"path": "a.py", "rel_path": "a.py"},
                    {"path": "b.py", "rel_path": "b.py"},
                ],
            },
        )
    )
    applied = events[-1]
    assert applied.phase == "applied"
    assert {c.path for c in applied.changes} == {"a.py", "b.py"}
    # change identities are preserved from the proposal, not regenerated.
    assert {c.change_id for c in applied.changes} == set(change_ids)


def test_never_proposed_write_emits_nothing_on_outcome() -> None:
    """approval_cb never reached (e.g. blocked before any proposal formed)."""
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="write_file", on_event=on_event
    )
    # wrap_approval_cb() is never called at all in this scenario.
    tracker.emit_outcome(
        ToolExecResult(ok=False, payload={"ok": False, "applied": False})
    )
    assert events == []


def test_internal_exception_after_proposal_emits_failed_once() -> None:
    events, on_event = _collector()
    tracker = FileEditLifecycleTracker(
        tool_call_id="call-1", tool_name="write_file", on_event=on_event
    )
    wrapped = tracker.wrap_approval_cb(lambda req: ApprovalDecision(action="approve"))
    wrapped(_write_req())

    tracker.emit_outcome_failed("internal_tool_error")
    # A second call (e.g. some other cleanup path) must not double-emit.
    tracker.emit_outcome_failed("internal_tool_error")

    phases = [e.phase for e in events]
    assert phases == ["proposed", "awaiting_approval", "failed"]
    assert events.count(events[-1]) == 1


def test_two_concurrent_trackers_never_cross_wire() -> None:
    """Two tool calls tracked independently never leak into each other's events.

    This is the property that replaces the old shared ``_last_event``
    correlation: each tracker owns its own closure and its own emitted
    events, so interleaving two trackers (as concurrent tasks would) cannot
    misattribute a proposal or outcome to the wrong tool call.
    """
    events_a, on_event_a = _collector()
    events_b, on_event_b = _collector()
    tracker_a = FileEditLifecycleTracker(
        tool_call_id="call-A", tool_name="write_file", on_event=on_event_a
    )
    tracker_b = FileEditLifecycleTracker(
        tool_call_id="call-B", tool_name="write_file", on_event=on_event_b
    )
    wrapped_a = tracker_a.wrap_approval_cb(lambda req: ApprovalDecision(action="approve"))
    wrapped_b = tracker_b.wrap_approval_cb(lambda req: ApprovalDecision(action="reject"))

    # Interleave calls the way two concurrently-dispatched tasks might.
    wrapped_a(_write_req("a.py"))
    wrapped_b(_write_req("b.py"))
    tracker_a.emit_outcome(
        ToolExecResult(ok=True, payload={"ok": True, "applied": True, "rel_path": "a.py"})
    )

    assert [e.phase for e in events_a] == ["proposed", "awaiting_approval", "applied"]
    assert [e.phase for e in events_b] == ["proposed", "awaiting_approval", "rejected"]
    assert all(e.tool_call_id == "call-A" for e in events_a)
    assert all(e.tool_call_id == "call-B" for e in events_b)
    assert all(c.path == "a.py" for e in events_a for c in e.changes)
    assert all(c.path == "b.py" for e in events_b for c in e.changes)
