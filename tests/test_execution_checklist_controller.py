from __future__ import annotations

from aura.events import (
    DISPATCH_CAMPAIGN_FINISHED,
    DISPATCH_CHECKLIST_DECLARED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
    WORKER_TOOL_FINISHED,
    WORKER_TOOL_STARTED,
    AuraEvent,
    EventBus,
)
from aura.execution_checklist import ExecutionChecklistController


def _controller_with_bus():
    bus = EventBus()
    snapshots: list[tuple[str, list[dict]]] = []
    controller = ExecutionChecklistController(bus)
    controller.set_on_change(lambda cid, rows: snapshots.append((cid, rows)))
    return bus, controller, snapshots


def _declare(bus: EventBus, *, campaign_id: str = "campaign-1") -> None:
    bus.emit(
        AuraEvent(
            topic=DISPATCH_CHECKLIST_DECLARED,
            campaign_id=campaign_id,
            payload={
                "items": [
                    {
                        "id": "create-helper",
                        "description": "Create helper module",
                        "owning_step_id": "step-1",
                        "files": ["src/helper.py"],
                    },
                    {
                        "id": "wire-caller",
                        "description": "Wire caller",
                        "owning_step_id": "step-2",
                    },
                ],
            },
        )
    )


def _statuses(controller: ExecutionChecklistController) -> list[str]:
    return [row["status"] for row in controller.snapshot("campaign-1")]


def test_checklist_declared_produces_all_pending_rows() -> None:
    bus, controller, snapshots = _controller_with_bus()

    _declare(bus)

    rows = controller.snapshot("campaign-1")
    assert [row["id"] for row in rows] == ["create-helper", "wire-caller"]
    assert [row["status"] for row in rows] == ["pending", "pending"]
    assert rows[0]["step_id"] == "step-1"
    assert rows[0]["files"] == ["src/helper.py"]
    assert len(snapshots) == 1


def test_step_started_marks_exactly_matching_row_active() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    _declare(bus)

    bus.emit(
        AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="campaign-1",
            step_id="step-1",
        )
    )

    assert _statuses(controller) == ["active", "pending"]


def test_step_completed_marks_matching_row_done() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    _declare(bus)

    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="step-1"))
    bus.emit(AuraEvent(topic=DISPATCH_STEP_COMPLETED, campaign_id="campaign-1", step_id="step-1"))

    assert _statuses(controller) == ["done", "pending"]


def test_multiple_rows_can_share_owner_and_complete_together() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    bus.emit(
        AuraEvent(
            topic=DISPATCH_CHECKLIST_DECLARED,
            campaign_id="campaign-1",
            payload={
                "items": [
                    {"id": "a", "description": "Move helper", "owning_step_id": "step-1"},
                    {"id": "b", "description": "Add tests", "owning_step_id": "step-1"},
                    {"id": "c", "description": "Wire caller", "owning_step_id": "step-2"},
                ],
            },
        )
    )

    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="step-1"))
    assert _statuses(controller) == ["active", "active", "pending"]

    bus.emit(AuraEvent(topic=DISPATCH_STEP_COMPLETED, campaign_id="campaign-1", step_id="step-1"))
    assert _statuses(controller) == ["done", "done", "pending"]


def test_unknown_step_id_falls_back_sequentially_only_when_safe() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    bus.emit(
        AuraEvent(
            topic=DISPATCH_CHECKLIST_DECLARED,
            campaign_id="campaign-1",
            payload={
                "items": [
                    {"id": "row-1", "description": "First ownerless row"},
                    {"id": "row-2", "description": "Second ownerless row"},
                ],
            },
        )
    )

    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="missing-1"))
    assert _statuses(controller) == ["active", "pending"]

    bus.emit(AuraEvent(topic=DISPATCH_STEP_COMPLETED, campaign_id="campaign-1", step_id="missing-1"))
    assert _statuses(controller) == ["done", "pending"]

    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="missing-2"))
    assert _statuses(controller) == ["done", "active"]

    bus.emit(
        AuraEvent(
            topic=DISPATCH_CHECKLIST_DECLARED,
            campaign_id="campaign-2",
            payload={
                "items": [
                    {"id": "mapped", "description": "Mapped row", "owning_step_id": "step-1"},
                    {"id": "other", "description": "Other mapped row", "owning_step_id": "step-2"},
                ],
            },
        )
    )
    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-2", step_id="unknown"))
    assert [row["status"] for row in controller.snapshot("campaign-2")] == ["pending", "pending"]


def test_campaign_finished_preserves_final_checked_list() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    _declare(bus)
    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="step-1"))
    bus.emit(AuraEvent(topic=DISPATCH_STEP_COMPLETED, campaign_id="campaign-1", step_id="step-1"))
    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="step-2"))
    bus.emit(AuraEvent(topic=DISPATCH_STEP_COMPLETED, campaign_id="campaign-1", step_id="step-2"))

    bus.emit(AuraEvent(topic=DISPATCH_CAMPAIGN_FINISHED, campaign_id="campaign-1"))

    rows = controller.snapshot("campaign-1")
    assert [row["id"] for row in rows] == ["create-helper", "wire-caller"]
    assert [row["status"] for row in rows] == ["done", "done"]


def test_campaign_finished_resolves_stale_active_and_pending_rows_on_success() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    _declare(bus)
    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="step-1"))

    bus.emit(
        AuraEvent(
            topic=DISPATCH_CAMPAIGN_FINISHED,
            campaign_id="campaign-1",
            payload={"ok": True},
        )
    )

    rows = controller.snapshot("campaign-1")
    assert [row["status"] for row in rows] == ["done", "done"]
    assert "active" not in {row["status"] for row in rows}
    assert "pending" not in {row["status"] for row in rows}


def test_campaign_finished_skips_unfinished_rows_on_failure() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    _declare(bus)
    bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, campaign_id="campaign-1", step_id="step-1"))

    bus.emit(
        AuraEvent(
            topic=DISPATCH_CAMPAIGN_FINISHED,
            campaign_id="campaign-1",
            payload={"ok": False},
        )
    )

    rows = controller.snapshot("campaign-1")
    assert [row["status"] for row in rows] == ["skipped", "skipped"]
    assert "active" not in {row["status"] for row in rows}
    assert "pending" not in {row["status"] for row in rows}


def test_active_campaign_declaration_does_not_replace_canonical_rows() -> None:
    bus, controller, _snapshots = _controller_with_bus()
    _declare(bus)

    bus.emit(
        AuraEvent(
            topic=DISPATCH_CHECKLIST_DECLARED,
            campaign_id="campaign-1",
            payload={
                "items": [
                    {
                        "id": "worker-row",
                        "description": "Worker-local replacement",
                    },
                ],
            },
        )
    )

    rows = controller.snapshot("campaign-1")
    assert [row["id"] for row in rows] == ["create-helper", "wire-caller"]


def test_worker_tool_events_do_not_alter_checklist_state() -> None:
    bus, controller, snapshots = _controller_with_bus()
    _declare(bus)
    initial = controller.snapshot("campaign-1")
    snapshots.clear()

    bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, run_id="campaign-1", payload={"name": "update_todo_list"}))
    bus.emit(
        AuraEvent(
            topic=WORKER_TOOL_FINISHED,
            run_id="campaign-1",
            payload={
                "name": "update_todo_list",
                "ok": True,
                "tasks": [{"id": "worker-row", "description": "Worker row"}],
            },
        )
    )
    bus.emit(
        AuraEvent(
            topic="worker.todo_list_updated",
            run_id="campaign-1",
            payload={"tasks": [{"id": "worker-row", "description": "Worker row"}]},
        )
    )

    assert controller.snapshot("campaign-1") == initial
    assert snapshots == []
