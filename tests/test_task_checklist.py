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


def test_registry_exposes_only_the_role_neutral_checklist_tool(tmp_path) -> None:
    names = {tool["function"]["name"] for tool in ToolRegistry(tmp_path).tool_defs()}

    assert UPDATE_TASK_CHECKLIST_TOOL in names


def test_projector_receives_checklist_fact_for_the_production_run() -> None:
    bus = EventBus()
    projector = TaskChecklistProjector(bus)
    bus.emit(AuraEvent(topic=TASK_CHECKLIST_UPDATED, run_id="prod-1", payload=_payload()))

    assert projector.snapshot_dicts("prod-1")[1]["id"] == "change"


def test_tool_description_establishes_live_progress_cursor_contract() -> None:
    """The model-facing description must make the checklist a live cursor.

    Bursty behavior (several items reported done together, long after they
    actually finished) comes from an underspecified contract, not from the
    projector or parser. This pins the semantics that fix it, without pinning
    the exact wording.
    """
    description = TASK_CHECKLIST_TOOL_DEF["function"]["description"].lower()

    # Single active item is the normal state while work remains.
    assert "exactly one item is" in description
    assert "active" in description

    # Initial snapshot: first item active, rest pending.
    assert "initial snapshot" in description
    assert "pending" in description

    # Prompt transition: mark done and activate the next item promptly.
    assert "promptly" in description
    assert "done" in description
    assert "activates the next" in description

    # No batching completed work for a later report.
    assert "do not batch" in description
    assert "report them together later" in description

    # Multiple items may complete together only for one atomic action.
    assert "atomic action" in description

    # Update at meaningful work boundaries, not on a timer.
    assert "work boundaries" in description

    # Pair bookkeeping with the next useful action instead of an empty round.
    assert "pair a checklist update" in description or "pair checklist" in description

    # Final active item is marked done at completion.
    assert "final snapshot" in description


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
