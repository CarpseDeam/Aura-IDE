"""Role-neutral checklist behavior for one production conversation."""
from __future__ import annotations

import json

from aura.bridge.execution_event_relay import ExecutionEventRelay
from aura.client import ToolResult
from aura.conversation.tools.registry import ToolRegistry
from aura.events import TASK_CHECKLIST_UPDATED, AuraEvent, EventBus
from aura.task_checklist import (
    UPDATE_TASK_CHECKLIST_TOOL,
    TaskChecklistProjector,
    parse_task_checklist_snapshot,
)


def _payload() -> dict:
    return {
        "items": [
            {"id": "inspect", "text": "Inspect the current implementation", "status": "done"},
            {"id": "change", "text": "Make the requested change", "status": "active"},
            {"id": "validate", "text": "Validate the result", "status": "pending"},
        ]
    }


class _ApprovalProxy:
    def consume_last_event(self):
        return None


def test_checklist_is_concise_progress_tracking_not_a_task_executor() -> None:
    snapshot, errors = parse_task_checklist_snapshot(_payload())

    assert errors == []
    assert snapshot is not None
    assert [item.id for item in snapshot.items] == ["inspect", "change", "validate"]


def test_checklist_allows_a_single_overall_progress_marker() -> None:
    snapshot, errors = parse_task_checklist_snapshot(
        {"items": [{"id": "answer", "text": "Answer the request", "status": "active"}]}
    )

    assert errors == []
    assert snapshot is not None


def test_registry_exposes_only_the_role_neutral_checklist_tool(tmp_path) -> None:
    names = {tool["function"]["name"] for tool in ToolRegistry(tmp_path).tool_defs()}

    assert UPDATE_TASK_CHECKLIST_TOOL in names


def test_projector_receives_checklist_fact_for_the_production_run() -> None:
    bus = EventBus()
    projector = TaskChecklistProjector(bus)
    bus.emit(AuraEvent(topic=TASK_CHECKLIST_UPDATED, run_id="prod-1", payload=_payload()))

    assert projector.snapshot_dicts("prod-1")[1]["id"] == "change"


def test_relay_emits_role_neutral_checklist_event() -> None:
    bus = EventBus()
    events: list[AuraEvent] = []
    bus.subscribe(TASK_CHECKLIST_UPDATED, events.append)
    relay = ExecutionEventRelay(_ApprovalProxy(), bus)

    relay.relay(
        "prod-1",
        ToolResult(
            tool_call_id="checklist-1",
            name=UPDATE_TASK_CHECKLIST_TOOL,
            ok=True,
            result=json.dumps({"ok": True, **_payload()}),
        ),
    )

    assert events[0].run_id == "prod-1"
    assert events[0].payload["tool_call_id"] == "checklist-1"
