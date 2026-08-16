"""End-to-end FileEditLifecycle coverage through the real write pipeline.

Drives ``ToolRoundRunner.run()`` with a real ``ToolRegistry``/``ToolRunner``
against a temp workspace, so these tests exercise the actual ``apply_patch``
routing (create/replace/patch/delete) through the real write handlers in
``_write_mixin.py`` / ``write_transaction.py`` / ``fs_write.py`` -- not a
fake. They prove:

* FileEditLifecycle events are correctly ordered relative to each other and
  to the existing ToolResult event.
* "applied" is only emitted once the write pipeline's own result proves
  ``applied is True``, and at that instant the event's content matches
  actual disk content (the projection-side invariant).
* Rejections and rolled-back/failed transactions never emit "applied" and
  never leave proposed content mistaken for committed content.
* Existing ToolResult / history contracts are untouched.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from aura.client import Event, FileEditLifecycle, ToolResult
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def _make_runner(tmp_path: Path) -> tuple[ToolRoundRunner, ToolRegistry, History]:
    history = History()
    tools = ToolRegistry(workspace_root=tmp_path)
    tool_runner = ToolRunner(history, tmp_path)
    runner = ToolRoundRunner(history=history, tools=tools, tool_runner=tool_runner)
    return runner, tools, history


def _run(
    runner: ToolRoundRunner, tool_calls: list[dict], approval_cb
) -> list[Event]:
    events: list[Event] = []
    runner.run(
        tool_calls=tool_calls,
        on_event=events.append,
        approval_cb=approval_cb,
        cancel_event=threading.Event(),
        cleanup_cancelled=lambda _cb: None,
    )
    return events


def _approve(_req) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def _reject(_req) -> ApprovalDecision:
    return ApprovalDecision(action="reject")


def _lifecycle_events(events: list[Event]) -> list[FileEditLifecycle]:
    return [e for e in events if isinstance(e, FileEditLifecycle)]


def _tool_results(events: list[Event]) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


def test_write_file_applied_lifecycle_matches_disk_at_emit_time(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("old = 1\n", encoding="utf-8")
    runner, _tools, history = _make_runner(tmp_path)

    events = _run(
        runner,
        [_tool_call("call-1", "apply_patch", {
            "operation": "replace", "path": "a.py", "content": "new = 2\n",
        })],
        _approve,
    )

    lifecycle = _lifecycle_events(events)
    assert [e.phase for e in lifecycle] == ["proposed", "awaiting_approval", "applied"]

    applied = lifecycle[-1]
    change = applied.changes[0]
    assert change.path == "a.py"
    assert change.action == "modify"
    # The instant "applied" is observed, disk content already matches it --
    # no lag between the event and the filesystem truth it describes.
    assert target.read_text(encoding="utf-8") == change.new_content == "new = 2\n"

    # Lifecycle events precede the paired ToolResult for the same call.
    results = _tool_results(events)
    assert len(results) == 1
    assert results[0].ok is True
    assert events.index(applied) < events.index(results[0])

    # Existing history contract: exactly one tool result appended.
    assert len(history.messages) == 1
    assert history.messages[0]["tool_call_id"] == "call-1"


def test_new_file_creation_lifecycle(tmp_path: Path) -> None:
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [_tool_call("call-1", "apply_patch", {
            "operation": "create", "path": "brand_new.py", "content": "x = 1\n",
        })],
        _approve,
    )

    applied = _lifecycle_events(events)[-1]
    assert applied.phase == "applied"
    assert applied.changes[0].action == "create"
    assert (tmp_path / "brand_new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_rejected_write_preserves_disk_and_emits_no_applied(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("old = 1\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [_tool_call("call-1", "apply_patch", {
            "operation": "replace", "path": "a.py", "content": "new = 2\n",
        })],
        _reject,
    )

    lifecycle = _lifecycle_events(events)
    assert [e.phase for e in lifecycle] == ["proposed", "awaiting_approval", "rejected"]
    assert target.read_text(encoding="utf-8") == "old = 1\n"


def test_approved_but_stale_write_becomes_failed_not_applied(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("old = 1\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    def approve_but_mutate(_req):
        # The file changes on disk while approval is "pending".
        target.write_text("raced = 3\n", encoding="utf-8")
        return ApprovalDecision(action="approve")

    events = _run(
        runner,
        [_tool_call("call-1", "apply_patch", {
            "operation": "replace", "path": "a.py", "content": "new = 2\n",
        })],
        approve_but_mutate,
    )

    lifecycle = _lifecycle_events(events)
    assert [e.phase for e in lifecycle] == ["proposed", "awaiting_approval", "failed"]
    assert lifecycle[-1].reason == "stale_approval"
    assert target.read_text(encoding="utf-8") == "raced = 3\n"


def test_delete_file_applied_lifecycle(tmp_path: Path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [_tool_call("call-1", "apply_patch", {
            "operation": "delete", "path": "gone.py", "reason": "cleanup",
        })],
        _approve,
    )

    lifecycle = _lifecycle_events(events)
    assert [e.phase for e in lifecycle] == ["proposed", "awaiting_approval", "applied"]
    assert lifecycle[-1].changes[0].action == "delete"
    assert not target.exists()


def test_multi_file_patch_updates_both_paths_with_stable_identities(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta = 1\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _tool_call(
                "call-1",
                "apply_patch",
                {
                    "operation": "patch",
                    "files": [
                        {"path": "a.py", "edits": [{"old": "alpha = 1", "new": "alpha = 2"}]},
                        {"path": "b.py", "edits": [{"old": "beta = 1", "new": "beta = 2"}]},
                    ]
                },
            )
        ],
        _approve,
    )

    lifecycle = _lifecycle_events(events)
    assert [e.phase for e in lifecycle] == ["proposed", "awaiting_approval", "applied"]

    proposed = lifecycle[0]
    change_ids = [c.change_id for c in proposed.changes]
    assert len(change_ids) == len(set(change_ids)) == 2

    applied = lifecycle[-1]
    paths = {c.path for c in applied.changes}
    assert paths == {"a.py", "b.py"}
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "alpha = 2\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "beta = 2\n"


def test_patch_transaction_commit_failure_never_reports_applied(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta = 1\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    # The second file's target changes underneath the transaction right when
    # approval is granted, so the freshness re-check just before commit fails
    # the whole transaction -- nothing is written for either file.
    def approve_and_race(_req):
        (tmp_path / "b.py").write_text("raced = 9\n", encoding="utf-8")
        return ApprovalDecision(action="approve")

    events = _run(
        runner,
        [
            _tool_call(
                "call-1",
                "apply_patch",
                {
                    "operation": "patch",
                    "files": [
                        {"path": "a.py", "edits": [{"old": "alpha = 1", "new": "alpha = 2"}]},
                        {"path": "b.py", "edits": [{"old": "beta = 1", "new": "beta = 2"}]},
                    ]
                },
            )
        ],
        approve_and_race,
    )

    lifecycle = _lifecycle_events(events)
    assert [e.phase for e in lifecycle] == ["proposed", "awaiting_approval", "failed"]
    # Neither file's proposed content was ever committed.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "raced = 9\n"


def test_auto_approved_write_still_emits_full_lifecycle(tmp_path: Path) -> None:
    """Auto-approve short-circuits the dialog, not the lifecycle."""
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [_tool_call("call-1", "apply_patch", {
            "operation": "create", "path": "auto.py", "content": "x = 1\n",
        })],
        _approve,  # stands in for an instant auto-approve decision
    )

    lifecycle = _lifecycle_events(events)
    assert [e.phase for e in lifecycle] == ["proposed", "awaiting_approval", "applied"]


def test_two_writes_in_one_round_do_not_cross_wire_paths(tmp_path: Path) -> None:
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _tool_call("call-1", "apply_patch", {
                "operation": "create", "path": "one.py", "content": "one\n",
            }),
            _tool_call("call-2", "apply_patch", {
                "operation": "create", "path": "two.py", "content": "two\n",
            }),
        ],
        _approve,
    )

    lifecycle = _lifecycle_events(events)
    by_call: dict[str, list[FileEditLifecycle]] = {}
    for ev in lifecycle:
        by_call.setdefault(ev.tool_call_id, []).append(ev)

    assert set(by_call) == {"call-1", "call-2"}
    assert [e.phase for e in by_call["call-1"]] == ["proposed", "awaiting_approval", "applied"]
    assert [e.phase for e in by_call["call-2"]] == ["proposed", "awaiting_approval", "applied"]
    assert all(c.path == "one.py" for e in by_call["call-1"] for c in e.changes)
    assert all(c.path == "two.py" for e in by_call["call-2"] for c in e.changes)
