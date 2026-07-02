"""Focused event-driven DispatchTodoController regression tests.

Every test constructs a fresh controller wired to an EventBus and drives
it by emitting dispatch-lifecycle events.  This exercises the subscription
and projection logic that the production dispatch path uses.
"""

from __future__ import annotations

from typing import Any

from aura.bridge.dispatch_todo_controller import DispatchTodoController
from aura.events import (
    DISPATCH_CAMPAIGN_FINISHED,
    DISPATCH_CHECKLIST_DECLARED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
    AuraEvent,
    EventBus,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _make_bus_and_controller():
    """Return ``(bus, controller, snapshots)``.

    *bus* — a fresh ``EventBus`` the controller already subscribes to.
    *controller* — a ``DispatchTodoController`` wired to *bus*.
    *snapshots* — a ``list[list[dict]]`` that receives every ``_on_change``
        snapshot, in order.
    """
    bus = EventBus()
    snapshots: list[list[dict[str, Any]]] = []
    ctrl = DispatchTodoController(event_bus=bus)
    ctrl.set_on_change(lambda _tid, tasks: snapshots.append(tasks))
    return bus, ctrl, snapshots


def _seed_objectives(bus: EventBus) -> None:
    """Emit a checklist_declared with three objectives for a two-step campaign."""
    bus.emit(AuraEvent(
        topic=DISPATCH_CHECKLIST_DECLARED,
        campaign_id="test-campaign",
        payload={
            "objectives": [
                {
                    "id": "todo-1",
                    "description": "Write the helper module",
                    "owning_step_id": "step-1",
                    "files": ["src/a.py"],
                },
                {
                    "id": "todo-2",
                    "description": "Wire the caller",
                    "owning_step_id": "step-2",
                },
                {
                    "id": "todo-3",
                    "description": "Add integration tests",
                    "owning_step_id": "step-2",
                },
            ],
        },
    ))


def _statuses(ctrl: DispatchTodoController) -> list[str]:
    """Return the status of every row in the controller snapshot."""
    return [r["status"] for r in ctrl.snapshot("test-campaign")]


# ── TestFullLifecycleProjection ────────────────────────────────────────────


class TestFullLifecycleProjection:
    """Project the full dispatch lifecycle through the event bus."""

    def test_checklist_seeds_pending_rows(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        _seed_objectives(bus)

        tasks = ctrl.snapshot("test-campaign")
        assert len(tasks) == 3
        assert all(t["status"] == "pending" for t in tasks)
        assert tasks[0]["id"] == "todo-1"
        assert tasks[1]["id"] == "todo-2"
        assert tasks[2]["id"] == "todo-3"
        # owning_step_id preserved
        assert tasks[0]["owning_step_id"] == "step-1"
        assert tasks[1]["owning_step_id"] == "step-2"
        assert tasks[2]["owning_step_id"] == "step-2"
        # files preserved only on the row that has them
        assert tasks[0].get("files") == ["src/a.py"]
        assert "files" not in tasks[1]
        assert "files" not in tasks[2]

    def test_step_started_activates_matching_rows(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        _seed_objectives(bus)
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))

        assert _statuses(ctrl) == ["active", "pending", "pending"]

    def test_step_completed_marks_matching_rows_done(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        _seed_objectives(bus)
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))

        assert _statuses(ctrl) == ["done", "pending", "pending"]

    def test_second_step_activates_remaining_rows(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        _seed_objectives(bus)
        # step-1: activate → complete
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        # step-2: activate → both remaining rows light up
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-2",
        ))

        assert _statuses(ctrl) == ["done", "active", "active"]

    def test_all_steps_completed_whole_rail_done(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        _seed_objectives(bus)
        # step-1
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        # step-2
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-2",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-2",
        ))

        assert _statuses(ctrl) == ["done", "done", "done"]

    def test_campaign_finished_snapshot_stable(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        _seed_objectives(bus)
        # Run both steps fully.
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-2",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-2",
        ))

        # Finish does not mutate row statuses — just finalises the rail.
        bus.emit(AuraEvent(
            topic=DISPATCH_CAMPAIGN_FINISHED,
            campaign_id="test-campaign",
        ))

        tasks = ctrl.snapshot("test-campaign")
        assert len(tasks) == 3
        assert [t["status"] for t in tasks] == ["done", "done", "done"]
        # IDs are preserved.
        assert tasks[0]["id"] == "todo-1"
        assert tasks[1]["id"] == "todo-2"
        assert tasks[2]["id"] == "todo-3"

    def test_on_change_fires_once_per_emitted_event(self) -> None:
        bus, ctrl, snapshots = _make_bus_and_controller()

        assert len(snapshots) == 0

        # 1. checklist_declared
        _seed_objectives(bus)
        assert len(snapshots) == 1, "checklist_declared → 1 callback"

        # 2. step_started (step-1)
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        assert len(snapshots) == 2, "step_started step-1 → 1 callback"

        # 3. step_completed (step-1)
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-1",
        ))
        assert len(snapshots) == 3, "step_completed step-1 → 1 callback"

        # 4. step_started (step-2)
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="test-campaign",
            step_id="step-2",
        ))
        assert len(snapshots) == 4, "step_started step-2 → 1 callback"

        # 5. step_completed (step-2)
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="test-campaign",
            step_id="step-2",
        ))
        assert len(snapshots) == 5, "step_completed step-2 → 1 callback"

        # 6. campaign_finished
        bus.emit(AuraEvent(
            topic=DISPATCH_CAMPAIGN_FINISHED,
            campaign_id="test-campaign",
        ))
        assert len(snapshots) == 6, "campaign_finished → 1 callback"


# ── TestOwnerlessFallbackViaEvents ─────────────────────────────────────────


class TestOwnerlessFallbackViaEvents:
    """Fallback logic when no row matches an emitted step_id."""

    # -- internal helper to seed rows without owning_step_id ----------------

    @staticmethod
    def _seed_ownerless(bus: EventBus, *, campaign_id: str = "tc-fallback",
                        count: int = 3) -> None:
        bus.emit(AuraEvent(
            topic=DISPATCH_CHECKLIST_DECLARED,
            campaign_id=campaign_id,
            payload={
                "objectives": [
                    {"id": f"r{i}", "description": f"Row {i}"}
                    for i in range(1, count + 1)
                ],
            },
        ))

    @staticmethod
    def _fallback_statuses(ctrl: DispatchTodoController,
                           campaign_id: str = "tc-fallback") -> list[str]:
        return [r["status"] for r in ctrl.snapshot(campaign_id)]

    # -- tests --------------------------------------------------------------

    def test_activate_unknown_step_falls_to_first_pending(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        self._seed_ownerless(bus)
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="unknown-step",
        ))

        assert self._fallback_statuses(ctrl) == ["active", "pending", "pending"]

    def test_complete_unknown_step_falls_to_active_row(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        self._seed_ownerless(bus)
        # Activate via fallback.
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="unknown-step-1",
        ))
        # Complete via fallback.
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="tc-fallback",
            step_id="unknown-step-1",
        ))

        assert self._fallback_statuses(ctrl) == ["done", "pending", "pending"]

    def test_repeated_fallback_advances_through_rows(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        self._seed_ownerless(bus)

        # 1. Activate → active on r1
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="u1",
        ))
        assert self._fallback_statuses(ctrl) == ["active", "pending", "pending"]

        # 2. Complete → r1 done
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="tc-fallback",
            step_id="u1",
        ))
        assert self._fallback_statuses(ctrl) == ["done", "pending", "pending"]

        # 3. Activate → r2 active
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="u2",
        ))
        assert self._fallback_statuses(ctrl) == ["done", "active", "pending"]

        # 4. Complete → r2 done
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="tc-fallback",
            step_id="u2",
        ))
        assert self._fallback_statuses(ctrl) == ["done", "done", "pending"]

        # 5. Activate → r3 active
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="u3",
        ))
        assert self._fallback_statuses(ctrl) == ["done", "done", "active"]

        # 6. Complete → r3 done
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="tc-fallback",
            step_id="u3",
        ))
        assert self._fallback_statuses(ctrl) == ["done", "done", "done"]

    def test_fallback_with_no_pending_rows_is_noop(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        # Seed only 1 row.
        self._seed_ownerless(bus, count=1)

        # Activate and complete the single row.
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="s1",
        ))
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_COMPLETED,
            campaign_id="tc-fallback",
            step_id="s1",
        ))
        assert self._fallback_statuses(ctrl) == ["done"]

        # Another step_started with no matching/pending rows — noop.
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="s2",
        ))

        # Status unchanged.
        assert self._fallback_statuses(ctrl) == ["done"]

    def test_fallback_with_done_at_front_skips_to_pending(self) -> None:
        bus, ctrl, _snapshots = _make_bus_and_controller()

        self._seed_ownerless(bus)
        # Force r1 done via direct method call (uses the same tool_call_id
        # that the seed established).
        ctrl.complete_step("tc-fallback", "r1")
        assert self._fallback_statuses(ctrl) == ["done", "pending", "pending"]

        # Emit step_started with unknown step_id — fallback should skip
        # already-done r1 and activate r2.
        bus.emit(AuraEvent(
            topic=DISPATCH_STEP_STARTED,
            campaign_id="tc-fallback",
            step_id="unknown",
        ))

        assert self._fallback_statuses(ctrl) == ["done", "active", "pending"]
