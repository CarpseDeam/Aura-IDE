"""Regression tests for WorkerEventRelay."""

from __future__ import annotations

from unittest.mock import Mock

from aura.bridge.event_relay import WorkerEventRelay
from aura.client.events import ToolCallArgsDelta, ToolCallEnd, ToolCallStart, ToolResult


def test_worker_event_relay_tool_call_lifecycle_tracks_index_to_id() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    starts: list[tuple[str, str, str]] = []
    args: list[tuple[str, str, str]] = []
    ends: list[tuple[str, str]] = []

    relay.toolCallStart.connect(lambda parent, worker_id, name: starts.append((parent, worker_id, name)))
    relay.toolCallArgs.connect(lambda parent, worker_id, chunk: args.append((parent, worker_id, chunk)))
    relay.toolCallEnd.connect(lambda parent, worker_id: ends.append((parent, worker_id)))

    relay.relay("dispatch-1", ToolCallStart(index=0, id="worker-tool-1", name="read_file"))
    relay.relay("dispatch-1", ToolCallArgsDelta(index=0, args_chunk='{"path": "a.py"}'))
    relay.relay("dispatch-1", ToolCallEnd(index=0))

    assert starts == [("dispatch-1", "worker-tool-1", "read_file")]
    assert args == [("dispatch-1", "worker-tool-1", '{"path": "a.py"}')]
    assert ends == [("dispatch-1", "worker-tool-1")]


def test_quality_bounce_is_tracked_separately_from_failures_and_writes() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    payload = (
        '{"ok": true, "applied": false, "quality_bounce": true, '
        '"path": "a.py", "tool_name": "edit_file", '
        '"repair_instructions": "Define missing", '
        '"craft_issues": [{"code": "undefined-name"}], '
        '"suggested_next_action": "Repair the proposed patch and retry this file."}'
    )

    relay.relay(
        "dispatch-1",
        ToolResult(tool_call_id="worker-tool-1", name="edit_file", ok=True, result=payload),
    )

    assert relay.quality_bounces == [
        {
            "path": "a.py",
            "tool_name": "edit_file",
            "repair_instructions": "Define missing",
            "craft_issues": [{"code": "undefined-name"}],
            "suggested_next_action": "Repair the proposed patch and retry this file.",
            "payload": {
                "ok": True,
                "applied": False,
                "quality_bounce": True,
                "path": "a.py",
                "tool_name": "edit_file",
                "repair_instructions": "Define missing",
                "craft_issues": [{"code": "undefined-name"}],
                "suggested_next_action": "Repair the proposed patch and retry this file.",
            },
        }
    ]
    assert relay.failed_tool_results == []
    assert relay.write_results == []
    assert relay.touched_files == set()
