"""Worker TODO snapshot model, tool, and relay tests."""

from __future__ import annotations

import json

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_activity import WorkerActivityController
from aura.client import ToolCallStart, ToolResult
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.registry import ToolRegistry
from aura.events import WORKER_TODO_UPDATED, AuraEvent, EventBus
from aura.worker_todo import (
    UPDATE_WORKER_TODO_TOOL,
    WorkerTodoProjector,
    parse_worker_todo_snapshot,
)


class _ApprovalProxy:
    def consume_last_event(self):
        return None


def _tool_names(tools: list[dict]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        fn = tool.get("function")
        if isinstance(fn, dict):
            names.add(str(fn.get("name") or ""))
    return names


def _snapshot_payload() -> dict:
    return {
        "items": [
            {"id": "inspect", "text": "Inspect current path", "status": "done"},
            {"id": "edit", "text": "Patch the implementation", "status": "active"},
            {"id": "verify", "text": "Run focused validation", "status": "pending"},
        ]
    }


class TestWorkerTodoModel:
    def test_valid_snapshot_parses(self) -> None:
        snapshot, errors = parse_worker_todo_snapshot(_snapshot_payload())

        assert errors == []
        assert snapshot is not None
        assert [item.id for item in snapshot.items] == ["inspect", "edit", "verify"]

    def test_rejects_multiple_active_items(self) -> None:
        payload = _snapshot_payload()
        payload["items"][0]["status"] = "active"

        snapshot, errors = parse_worker_todo_snapshot(payload)

        assert snapshot is None
        assert any("only one item" in error or "exactly one" in error for error in errors)

    def test_all_done_snapshot_may_have_no_active_item(self) -> None:
        payload = _snapshot_payload()
        for item in payload["items"]:
            item["status"] = "done"

        snapshot, errors = parse_worker_todo_snapshot(payload)

        assert errors == []
        assert snapshot is not None


class TestWorkerTodoTool:
    def test_worker_catalog_exposes_update_worker_todo(self) -> None:
        tools = ToolCatalog().build_tool_defs(mode="worker", read_only=False)

        assert UPDATE_WORKER_TODO_TOOL in _tool_names(tools)

    def test_planner_catalog_does_not_expose_update_worker_todo(self) -> None:
        tools = ToolCatalog().build_tool_defs(mode="planner", read_only=False)

        assert UPDATE_WORKER_TODO_TOOL not in _tool_names(tools)

    def test_tool_handler_returns_normalized_snapshot(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path, mode="worker")

        result = registry.execute(
            UPDATE_WORKER_TODO_TOOL,
            _snapshot_payload(),
            approval_cb=None,
        )

        assert result.ok is True
        assert result.payload["ok"] is True
        assert result.payload["items"][1]["status"] == "active"
        assert result.extras["worker_todo"] is True


class TestWorkerTodoProjector:
    def test_projector_stores_and_emits_full_snapshot(self) -> None:
        bus = EventBus()
        projector = WorkerTodoProjector(bus)
        changes: list[tuple[str, list[dict[str, str]]]] = []
        projector.set_on_change(lambda run_id, items: changes.append((run_id, items)))

        bus.emit(AuraEvent(
            topic=WORKER_TODO_UPDATED,
            run_id="dispatch-1",
            payload=_snapshot_payload(),
        ))

        assert projector.snapshot_dicts("dispatch-1")[1]["id"] == "edit"
        assert changes == [("dispatch-1", projector.snapshot_dicts("dispatch-1"))]


class TestWorkerTodoRelay:
    def test_successful_tool_result_emits_worker_todo_event(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []
        bus.subscribe(WORKER_TODO_UPDATED, received.append)
        relay = WorkerEventRelay(approval_proxy=_ApprovalProxy(), event_bus=bus)

        relay.relay("dispatch-1", ToolResult(
            tool_call_id="todo-tool-1",
            name=UPDATE_WORKER_TODO_TOOL,
            ok=True,
            result=json.dumps({"ok": True, **_snapshot_payload()}),
            extras={},
        ))

        assert len(received) == 1
        assert received[0].run_id == "dispatch-1"
        assert received[0].payload["items"][1]["status"] == "active"
        assert received[0].payload["worker_tool_id"] == "todo-tool-1"

    def test_worker_todo_tool_does_not_emit_activity_entries(self) -> None:
        bus = EventBus()
        activity = WorkerActivityController(bus)
        relay = WorkerEventRelay(approval_proxy=_ApprovalProxy(), event_bus=bus)

        relay.relay("dispatch-1", ToolCallStart(
            index=0,
            id="todo-tool-1",
            name=UPDATE_WORKER_TODO_TOOL,
        ))
        relay.relay("dispatch-1", ToolResult(
            tool_call_id="todo-tool-1",
            name=UPDATE_WORKER_TODO_TOOL,
            ok=True,
            result=json.dumps({"ok": True, **_snapshot_payload()}),
            extras={},
        ))

        assert activity.snapshot() == []
