"""Tests for WorkerActivityController — activity entry tracking via the event bus."""

from __future__ import annotations

from aura.bridge.worker_activity import WorkerActivityController
from aura.events import (
    DISPATCH_CAMPAIGN_FINISHED,
    DISPATCH_CAMPAIGN_STARTED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
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

    def test_campaign_started_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=DISPATCH_CAMPAIGN_STARTED,
            payload={"goal": "Fix the bug", "step_count": 3},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "campaign_started"

    def test_step_started_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            payload={"step_id": "step-1", "description": "Add tests"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "step_started"

    def test_tool_started_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORKER_TOOL_STARTED,
            payload={"name": "read_file", "args": '{"path": "a.py"}'},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "tool_started"

    def test_step_completed_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            payload={"step_id": "step-1", "ok": True},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "step_completed"

    def test_worker_failed_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORKER_FAILED,
            payload={"error": "Something broke"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "worker_failed"

    def test_file_changed_appends_entry(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=WORKER_FILE_CHANGED,
            payload={"path": "src/a.py", "action": "edit"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "file_changed"

    def test_multiple_events_all_recorded(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=DISPATCH_CAMPAIGN_STARTED, payload={"goal": "g"}))
        bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, payload={"step_id": "s1"}))
        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "read"}))

        assert len(ctrl.snapshot()) == 3

    def test_clear_removes_entries(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=DISPATCH_CAMPAIGN_STARTED, payload={"goal": "g"}))
        assert len(ctrl.snapshot()) == 1

        ctrl.clear()
        assert ctrl.snapshot() == []

    def test_snapshot_dicts_returns_dicts(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=DISPATCH_CAMPAIGN_STARTED, payload={"goal": "g"}))

        dicts = ctrl.snapshot_dicts()
        assert len(dicts) == 1
        assert dicts[0]["kind"] == "campaign_started"
        assert "message" in dicts[0]


# ── TestComprehensiveActivityFlow ───────────────────────────────────────────


class TestComprehensiveActivityFlow:
    """Comprehensive state-machine activity flow tests."""

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _topic_ok(ok: bool, kind: str) -> tuple[str, str]:
        """Return the expected kind suffix given `ok` truthiness."""
        if ok is not False:
            return ("completed" if kind == "tool" else "passed" if kind == "validation" else "completed", "ok" in (True, None) or ok is True)  # noqa: E501
        return ("failed", False)

    # ── comprehensive flow ─────────────────────────────────────────────────

    def test_full_sequential_activity_log(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        # 1. Campaign started
        bus.emit(AuraEvent(
            topic=DISPATCH_CAMPAIGN_STARTED,
            run_id="run-1",
            campaign_id="campaign-1",
            payload={"goal": "Refactor auth"},
        ))

        # 2. Step started
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            run_id="run-1",
            campaign_id="campaign-1",
            step_id="step-1",
            payload={"step_id": "step-1", "description": "Create module"},
        ))

        # 3. Tool started
        bus.emit(AuraEvent(
            topic=WORKER_TOOL_STARTED,
            run_id="run-1",
            payload={"name": "write_file"},
        ))

        # 4. File changed
        bus.emit(AuraEvent(
            topic=WORKER_FILE_CHANGED,
            run_id="run-1",
            payload={"path": "src/auth.py", "action": "created"},
        ))

        # 5. Tool finished (ok=True)
        bus.emit(AuraEvent(
            topic=WORKER_TOOL_FINISHED,
            run_id="run-1",
            payload={"name": "write_file", "ok": True},
        ))

        # 6. Command started
        bus.emit(AuraEvent(
            topic=WORKER_COMMAND_STARTED,
            run_id="run-1",
            payload={"command": "python -m compileall src/"},
        ))

        # 7. Command finished (exit_code=0)
        bus.emit(AuraEvent(
            topic=WORKER_COMMAND_FINISHED,
            run_id="run-1",
            payload={"command": "python -m compileall src/", "exit_code": 0},
        ))

        # 8. Validation started
        bus.emit(AuraEvent(
            topic=WORKER_VALIDATION_STARTED,
            run_id="run-1",
            payload={"label": "ruff check"},
        ))

        # 9. Validation finished (ok=True)
        bus.emit(AuraEvent(
            topic=WORKER_VALIDATION_FINISHED,
            run_id="run-1",
            payload={"label": "ruff check", "ok": True},
        ))

        # 10. Step completed
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            run_id="run-1",
            campaign_id="campaign-1",
            step_id="step-1",
            payload={"step_id": "step-1", "ok": True},
        ))

        # 11. Final report started
        bus.emit(AuraEvent(
            topic=WORKER_FINAL_REPORT_STARTED,
            run_id="run-1",
        ))

        # 12. Final report finished
        bus.emit(AuraEvent(
            topic=WORKER_FINAL_REPORT_FINISHED,
            run_id="run-1",
            payload={"ok": True},
        ))

        # 13. Campaign finished — NOT subscribed, should be ignored
        bus.emit(AuraEvent(
            topic=DISPATCH_CAMPAIGN_FINISHED,
            run_id="run-1",
            campaign_id="campaign-1",
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 12, f"Expected 12 entries, got {len(entries)}"

        # Ordered kind assertions
        expected_kinds = [
            "campaign_started",
            "step_started",
            "tool_started",
            "file_changed",
            "tool_completed",
            "command_started",
            "command_finished",
            "validation_started",
            "validation_passed",
            "step_completed",
            "final_report_started",
            "final_report_completed",
        ]
        for idx, kind in enumerate(expected_kinds):
            assert entries[idx].kind == kind, (
                f"Entry {idx}: expected kind={kind!r}, got {entries[idx].kind!r}"
            )

        # Specific content assertions
        assert "Refactor auth" in entries[0].message
        assert "Create module" in entries[1].message
        assert entries[2].detail == "write_file"
        assert "src/auth.py" in entries[3].message
        assert "exit 0" in entries[6].message

    # ── identity fields ────────────────────────────────────────────────────

    def test_identity_fields_propagated(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            run_id="r2",
            campaign_id="c2",
            step_id="s2",
            payload={"step_id": "s2"},
        ))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].run_id == "r2"
        assert entries[0].campaign_id == "c2"
        assert entries[0].step_id == "s2"

    # ── on_change callback ─────────────────────────────────────────────────

    def test_on_change_callback_invoked_per_append(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        snapshots: list[list] = []
        ctrl.set_on_change(lambda snap: snapshots.append(list(snap)))

        bus.emit(AuraEvent(
            topic=DISPATCH_CAMPAIGN_STARTED,
            payload={"goal": "g1"},
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            payload={"step_id": "s1"},
        ))
        bus.emit(AuraEvent(
            topic=WORKER_TOOL_STARTED,
            payload={"name": "read"},
        ))

        assert len(snapshots) == 3, f"Expected 3 callback invocations, got {len(snapshots)}"
        assert len(snapshots[-1]) == 3

    # ── no TODO references ─────────────────────────────────────────────────

    def test_no_todo_references(self) -> None:
        """WorkerActivityController module must not import TODO types."""
        # Direct import attempts should fail with ImportError
        from aura.bridge import worker_activity as wa_mod

        # DispatchTodoRow should not exist in the worker_activity module's
        # namespace.
        assert not hasattr(wa_mod, "DispatchTodoRow"), (
            "worker_activity module must not expose DispatchTodoRow"
        )

        try:
            from aura.bridge.worker_activity import DispatchTodoRow  # noqa: F401
            assert False, "DispatchTodoRow should not be importable"
        except ImportError:
            pass
