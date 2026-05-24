"""Regression tests for WorkerEventRelay."""

from __future__ import annotations

from unittest.mock import Mock

from aura.bridge.event_relay import WorkerEventRelay
from aura.client.events import ToolCallArgsDelta, ToolCallEnd, ToolCallStart


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
