"""Tests for WorkerActivityController — activity entry tracking via the event bus."""

from __future__ import annotations

from aura.bridge.worker_activity import WorkerActivityController
from aura.events import (
    WORK_ARTIFACT_ITEM_COMPLETED,
    WORK_ARTIFACT_ITEM_READY,
    WORKER_COMMAND_FINISHED,
    WORKER_COMMAND_STARTED,
    WORKER_FAILED,
    WORKER_FILE_CHANGED,
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_TOOL_FINISHED,
    WORKER_TOOL_STARTED,
    WORKER_VALIDATION_FINISHED,
    WORKER_VALIDATION_STARTED,
    AuraEvent,
    EventBus,
)

# ── TestWorkerActivityController ───────────────────────────────────────────


class TestWorkerActivityController:
    """Minimal WorkerActivityController tests."""

    def test_controller_created_empty(self) -> None:
        ctrl = WorkerActivityController(EventBus())
        assert ctrl.snapshot() == []

    def test_artifact_item_ready_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORK_ARTIFACT_ITEM_READY,
            artifact_id="art-1",
            artifact_item_id="item-1",
            payload={"title": "Fix auth", "item_id": "item-1"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "artifact_item_ready"

    def test_artifact_item_completed_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORK_ARTIFACT_ITEM_COMPLETED,
            artifact_id="art-1",
            artifact_item_id="item-1",
            payload={"title": "Fix auth", "status": "done", "item_id": "item-1"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "artifact_item_done"

    def test_tool_started_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORKER_TOOL_STARTED,
            payload={"name": "read_file"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "tool_started"

    def test_tool_finished_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORKER_TOOL_FINISHED,
            payload={"name": "read_file", "ok": True},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "tool_completed"

    def test_caps_history(self) -> None:
        ctrl = WorkerActivityController(EventBus(), maxlen=3)

        for i in range(5):
            ctrl._append(type("Entry", (), {"kind": "t", "message": f"e{i}", "to_dict": lambda self: {}})())  # noqa

        assert len(ctrl.snapshot()) <= 3

    def test_file_changed_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORKER_FILE_CHANGED,
            payload={"path": "src/main.py", "action": "modified"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "file_changed"

    def test_command_started_and_finished(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_COMMAND_STARTED, payload={"command": "npm test"}))
        bus.emit(AuraEvent(topic=WORKER_COMMAND_FINISHED, payload={"exit_code": 0}))

        entries = ctrl.snapshot()
        assert len(entries) == 2
        assert entries[0].kind == "command_started"
        assert entries[1].kind == "command_finished"

    def test_validation_started_and_finished(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_VALIDATION_STARTED, payload={"command": "pytest"}))
        bus.emit(AuraEvent(topic=WORKER_VALIDATION_FINISHED, payload={"ok": True}))

        entries = ctrl.snapshot()
        assert len(entries) == 2
        assert entries[0].kind == "validation_started"
        assert entries[1].kind == "validation_passed"

    def test_final_report_events(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_FINAL_REPORT_STARTED, payload={}))
        bus.emit(AuraEvent(topic=WORKER_FINAL_REPORT_FINISHED, payload={"ok": True}))

        entries = ctrl.snapshot()
        assert len(entries) == 2
        assert entries[0].kind == "final_report_started"
        assert entries[1].kind == "final_report_completed"

    def test_worker_failed_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_FAILED, payload={"error": "timeout"}))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "worker_failed"
        assert "timeout" in entries[0].message

    def test_clear_removes_all_entries(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "x"}))
        assert len(ctrl.snapshot()) == 1
        ctrl.clear()
        assert len(ctrl.snapshot()) == 0

    def test_on_change_callback(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)
        snapshots: list[list] = []
        ctrl.set_on_change(lambda s: snapshots.append(s))

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "a"}))

        assert len(snapshots) == 1
