"""Role-neutral checklist behavior for one production conversation."""
from __future__ import annotations

import json

from aura.bridge.execution_event_relay import ExecutionEventRelay
from aura.client import ToolResult
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools.schemas.checklist import TASK_CHECKLIST_TOOL_DEF
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


def test_checklist_accepts_more_than_seven_coherent_items() -> None:
    """The checklist is a display-only progress indicator, not a planner —
    it must not carry an artificial item-count ceiling."""
    payload = {
        "items": [
            {"id": f"step-{i}", "text": f"Do step {i}", "status": "pending"}
            for i in range(12)
        ]
    }
    snapshot, errors = parse_task_checklist_snapshot(payload)

    assert errors == []
    assert snapshot is not None
    assert len(snapshot.items) == 12
    assert [item.id for item in snapshot.items] == [f"step-{i}" for i in range(12)]


def test_checklist_text_is_not_silently_cut_by_hidden_parser_limits() -> None:
    long_id = "guard-" + "x" * 200
    long_text = "Investigate and repair " + "y" * 400
    payload = {"items": [{"id": long_id, "text": long_text, "status": "active"}]}

    snapshot, errors = parse_task_checklist_snapshot(payload)

    assert errors == []
    assert snapshot is not None
    assert snapshot.items[0].id == long_id
    assert snapshot.items[0].text == long_text


def test_checklist_schema_has_no_maxitems_ceiling() -> None:
    parameters = TASK_CHECKLIST_TOOL_DEF["function"]["parameters"]
    items_schema = parameters["properties"]["items"]

    assert "maxItems" not in items_schema


def test_registry_exposes_only_the_role_neutral_checklist_tool(tmp_path) -> None:
    names = {tool["function"]["name"] for tool in ToolRegistry(tmp_path).tool_defs()}

    assert UPDATE_TASK_CHECKLIST_TOOL in names


def test_projector_projects_more_than_seven_items_correctly() -> None:
    bus = EventBus()
    projector = TaskChecklistProjector(bus)
    payload = {
        "items": [
            {"id": f"step-{i}", "text": f"Do step {i}", "status": "pending"}
            for i in range(10)
        ]
    }
    bus.emit(AuraEvent(topic=TASK_CHECKLIST_UPDATED, run_id="prod-2", payload=payload))

    snapshot = projector.snapshot_dicts("prod-2")
    assert len(snapshot) == 10
    assert [item["id"] for item in snapshot] == [f"step-{i}" for i in range(10)]


def test_projector_receives_checklist_fact_for_the_production_run() -> None:
    bus = EventBus()
    projector = TaskChecklistProjector(bus)
    bus.emit(AuraEvent(topic=TASK_CHECKLIST_UPDATED, run_id="prod-1", payload=_payload()))

    assert projector.snapshot_dicts("prod-1")[1]["id"] == "change"


def test_tool_description_establishes_live_progress_cursor_contract() -> None:
    """The model-facing description keeps the checklist a live cursor, briefly.

    Phase 2C shortened this schema description on purpose (fewer sentences,
    same semantics): one active item at a time, a full-snapshot replacement
    contract, display-only, and never a separate execution context. This pins
    the surviving semantics without pinning the old verbose wording.
    """
    description = TASK_CHECKLIST_TOOL_DEF["function"]["description"].lower()

    # Single active item is the normal state while work remains.
    assert "active" in description
    assert "one item" in description

    # Full-snapshot replacement contract.
    assert "pending" in description
    assert "done" in description
    assert "full ordered list" in description or "replacing the previous" in description

    # Never a separate execution context.
    assert "phases" in description or "phase" in description
    assert "execution context" in description

    # Display-only: it never blocks or gates the task.
    assert "never" in description
    assert "block" in description or "gate" in description

    # Skipped for trivial requests, not mandatory bookkeeping.
    assert "trivial" in description


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
