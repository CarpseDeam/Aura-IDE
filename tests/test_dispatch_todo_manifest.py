"""Tests for dispatch TODO manifest filtering, fallback, and controller."""

from aura.bridge.dispatch_todo_controller import DispatchTodoController
from aura.conversation.dispatch_plan import WorkerDispatchPlan, WorkerStepSpec
from aura.conversation.dispatch_todo_manifest import (
    DispatchTodoItem,
    _is_implementation_detail,
    todo_tasks_from_plan,
)

# ── _is_implementation_detail ─────────────────────────────────────────────

def test_implementation_detail_filter():
    """Junk / code-declaration texts return True; legitimate work items return False."""
    # --- junk: should be filtered out (return True) ---
    assert _is_implementation_detail("") is True
    assert _is_implementation_detail("  ") is True
    assert _is_implementation_detail("from os import path") is True
    assert _is_implementation_detail("import sys") is True
    assert _is_implementation_detail("from __future__ import annotations") is True
    assert _is_implementation_detail('"""Some docstring."""') is True
    assert _is_implementation_detail("full_message: dict[str, Any] | None = None") is True
    assert _is_implementation_detail("(see above)") is True
    assert _is_implementation_detail("|-=-=-=") is True
    assert _is_implementation_detail("| None = None") is True
    assert _is_implementation_detail("_internal_helper") is True

    # --- legitimate: should survive (return False) ---
    assert _is_implementation_detail("Create the new helper module") is False
    assert _is_implementation_detail("Wire the caller with imports") is False
    assert _is_implementation_detail("Add validation for edge cases") is False
    assert _is_implementation_detail("Run integration smoke tests") is False


# ── todo_tasks_from_plan with visible_checklist ──────────────────────────

def _two_step_plan(*, checklist_items: list[DispatchTodoItem]) -> WorkerDispatchPlan:
    return WorkerDispatchPlan(
        overall_goal="Refactor shell pipeline helpers.",
        visible_summary="Extract helpers and wire caller.",
        steps=[
            WorkerStepSpec(
                id="step-1",
                title="Create helper module",
                goal="Move helpers to _shell_pipeline.py",
                spec="Extract the helper functions.",
                files=["aura/bridge/_shell_pipeline.py"],
                acceptance="Helpers are in the module.",
            ),
            WorkerStepSpec(
                id="step-2",
                title="Wire caller",
                goal="Import helpers from the module",
                spec="Remove local copies and import.",
                files=["aura/bridge/worker_completion_result.py"],
                acceptance="Caller imports from the module.",
            ),
        ],
        visible_checklist=checklist_items,
    )


def test_todo_tasks_from_plan_with_checklist():
    """Visible checklist drives row count and ordering; active/completed lighting works."""
    items = [
        DispatchTodoItem(
            id="todo-a",
            description="Write _shell_pipeline.py",
            owning_step_id="step-1",
        ),
        DispatchTodoItem(
            id="todo-b",
            description="Add unit tests for helpers",
            owning_step_id="step-1",
        ),
        DispatchTodoItem(
            id="todo-c",
            description="Remove local helper definitions",
            owning_step_id="step-2",
        ),
        DispatchTodoItem(
            id="todo-d",
            description="Update imports in caller",
            owning_step_id="step-2",
        ),
    ]
    plan = _two_step_plan(checklist_items=items)

    # --- no lighting: all pending ---
    tasks = todo_tasks_from_plan(plan)
    assert len(tasks) == 4, f"Expected 4 rows, got {len(tasks)}"
    assert [t["id"] for t in tasks] == ["todo-a", "todo-b", "todo-c", "todo-d"]
    assert all(t["status"] == "pending" for t in tasks)

    # --- active_step_id lights matching rows ---
    tasks_active = todo_tasks_from_plan(plan, active_step_id="step-1")
    assert len(tasks_active) == 4
    assert tasks_active[0]["status"] == "active"  # todo-a, step-1
    assert tasks_active[1]["status"] == "active"  # todo-b, step-1
    assert tasks_active[2]["status"] == "pending"  # todo-c, step-2
    assert tasks_active[3]["status"] == "pending"  # todo-d, step-2

    # --- completed_step_ids lights matching rows as done ---
    tasks_done = todo_tasks_from_plan(plan, completed_step_ids={"step-1"})
    assert len(tasks_done) == 4
    assert tasks_done[0]["status"] == "done"  # todo-a, step-1
    assert tasks_done[1]["status"] == "done"  # todo-b, step-1
    assert tasks_done[2]["status"] == "pending"  # todo-c, step-2
    assert tasks_done[3]["status"] == "pending"  # todo-d, step-2


# ── todo_tasks_from_plan fallback (empty visible_checklist) ───────────────

def test_todo_tasks_from_plan_fallback():
    """Empty visible_checklist falls back to one row per step."""
    plan = WorkerDispatchPlan(
        overall_goal="Refactor shell pipeline helpers.",
        steps=[
            WorkerStepSpec(
                id="step-1",
                title="Create helper module",
                goal="Move helpers to _shell_pipeline.py",
                spec="Extract the helper functions.",
            ),
            WorkerStepSpec(
                id="step-2",
                title="Wire caller",
                goal="Import helpers from the module",
                spec="Remove local copies and import.",
            ),
        ],
        visible_checklist=[],
    )

    tasks = todo_tasks_from_plan(plan)
    assert len(tasks) == 2, f"Expected 2 fallback rows, got {len(tasks)}"
    assert tasks[0]["id"] == "step-1"
    assert tasks[1]["id"] == "step-2"
    assert tasks[0]["owning_step_id"] == "step-1"
    assert tasks[1]["owning_step_id"] == "step-2"


# ── DispatchTodoController ownerless fallback ────────────────────────────

def test_dispatch_todo_controller_ownerless_fallback():
    """Controller activates next pending / completes active when step_id does not match."""
    ctrl = DispatchTodoController()

    result = ctrl.begin("test", [
        {"id": "task-1", "description": "First task"},
        {"id": "task-2", "description": "Second task"},
        {"id": "task-3", "description": "Third task"},
    ])
    assert result is not None
    # begin returns snapshot with all pending
    assert [r["status"] for r in result] == ["pending", "pending", "pending"]

    # Activate with non-matching step_id → fallback activates first pending row
    snap = ctrl.activate_step("test", "step-99")
    assert snap is not None
    assert [r["status"] for r in snap] == ["active", "pending", "pending"]

    # Complete with non-matching step_id → fallback completes the active row
    snap = ctrl.complete_step("test", "step-99")
    assert snap is not None
    assert [r["status"] for r in snap] == ["done", "pending", "pending"]
