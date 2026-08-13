from aura.bridge.execution_activity import ExecutionActivityController
from aura.events import (
    EXECUTION_TOOL_FINISHED,
    EXECUTION_TOOL_STARTED,
    AuraEvent,
    EventBus,
)


def test_execution_activity_projects_tool_lifecycle_for_one_run() -> None:
    bus = EventBus()
    controller = ExecutionActivityController(bus)

    bus.emit(AuraEvent(topic=EXECUTION_TOOL_STARTED, run_id="prod-1", payload={"name": "read_file"}))
    bus.emit(AuraEvent(topic=EXECUTION_TOOL_FINISHED, run_id="prod-1", payload={"name": "read_file", "ok": True}))

    entries = controller.snapshot()
    assert [entry.kind for entry in entries] == ["tool_started", "tool_completed"]
    assert {entry.run_id for entry in entries} == {"prod-1"}
