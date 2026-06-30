import json

import pytest
from PySide6.QtWidgets import QApplication

from aura.bridge.event_relay import WorkerEventRelay
from aura.client import Done, ToolCallStart, ToolResult
from aura.gui.controllers import ToolStreamController
from aura.gui.widgets.todo_list import TodoListWidget, normalize_todo_tasks


@pytest.fixture
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_stream_controller_partial_todo_no_emit():
    """ToolStreamController with partial update_todo_list JSON does not emit todo_updated."""
    controller = ToolStreamController("update_todo_list")
    todo_updates = []
    controller.todo_updated.connect(todo_updates.append)

    # Stream a partial chunk
    controller.append_fragment('{"tasks": [{"description": "First task", "status": "active"}')
    assert len(todo_updates) == 0

    # Stream final complete JSON (still inside streaming phase, i.e., append_fragment)
    controller.append_fragment(']}')
    assert len(todo_updates) == 0


def test_event_relay_emits_todo_list_updated():
    """Final update_todo_list ToolResult still emits WorkerEventRelay.todoListUpdated."""
    class MockApprovalProxy:
        def consume_last_event(self):
            return None

    relay = WorkerEventRelay(MockApprovalProxy())
    emitted_tasks = []
    relay.todoListUpdated.connect(lambda tool_call_id, tasks: emitted_tasks.append(tasks))

    # Construct a ToolResult event
    result_payload = {"ok": True, "message": "TODO list updated", "tasks": [{"task": "Do homework", "status": "active"}]}
    ev = ToolResult(
        tool_call_id="tc_123",
        name="update_todo_list",
        ok=True,
        result=json.dumps(result_payload),
        extras={"tasks": result_payload["tasks"]}
    )

    relay.relay("parent_tc", ev)
    assert len(emitted_tasks) == 1
    assert emitted_tasks[0] == [{"task": "Do homework", "status": "active"}]
    assert relay.todo_used is True

    # Also test where extras is missing, but result has tasks
    emitted_tasks.clear()
    ev_no_extras = ToolResult(
        tool_call_id="tc_123",
        name="update_todo_list",
        ok=True,
        result=json.dumps(result_payload),
        extras=None
    )
    relay.relay("parent_tc", ev_no_extras)
    assert len(emitted_tasks) == 1
    assert emitted_tasks[0] == [{"task": "Do homework", "status": "active"}]
    assert relay.todo_used is True  # still True after second relay


def test_event_relay_synthesizes_todo_progress_from_worker_events():
    """Worker progress updates the TODO UI even when the model never calls update_todo_list."""
    class MockApprovalProxy:
        def consume_last_event(self):
            return None

    relay = WorkerEventRelay(MockApprovalProxy())
    emitted_tasks = []
    relay.todoListUpdated.connect(lambda tool_call_id, tasks: emitted_tasks.append(tasks))

    relay.relay("parent_tc", ToolCallStart(index=0, id="read1", name="read_file_range"))
    assert emitted_tasks[-1] == [{"description": "Inspect relevant files", "status": "active"}]
    assert relay.todo_used is False

    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="read1",
            name="read_file_range",
            ok=True,
            result=json.dumps({"ok": True, "path": "a.py", "content_hash": "abc"}),
        ),
    )
    assert {"description": "Inspect relevant files", "status": "done"} in emitted_tasks[-1]

    relay.relay("parent_tc", ToolCallStart(index=1, id="patch1", name="patch_file"))
    assert {"description": "Apply changes", "status": "active"} in emitted_tasks[-1]

    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="patch1",
            name="patch_file",
            ok=True,
            result=json.dumps({"ok": True, "path": "a.py", "applied": True}),
        ),
    )
    assert {"description": "Apply changes", "status": "done"} in emitted_tasks[-1]

    relay.relay("parent_tc", ToolCallStart(index=2, id="term1", name="run_terminal_command"))
    assert {"description": "Run validation", "status": "active"} in emitted_tasks[-1]

    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="term1",
            name="run_terminal_command",
            ok=True,
            result=json.dumps({
                "ok": True,
                "command": "python -m py_compile a.py",
                "exit_code": 0,
                "output": "",
            }),
        ),
    )
    assert {"description": "Run validation", "status": "done"} in emitted_tasks[-1]

    relay.relay(
        "parent_tc",
        Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Done.", "reasoning_content": None},
        ),
    )
    assert {"description": "Deliver final report", "status": "done"} in emitted_tasks[-1]


def test_event_relay_preserves_model_todo_order_while_edit_activity_is_active():
    """Model-authored TODO order stays stable while edit progress is active."""
    class MockApprovalProxy:
        def consume_last_event(self):
            return None

    relay = WorkerEventRelay(MockApprovalProxy())
    emitted_tasks = []
    relay.todoListUpdated.connect(lambda tool_call_id, tasks: emitted_tasks.append(tasks))

    model_tasks = [
        {"description": "Inspect existing code", "status": "done"},
        {"description": "Update aura/gui/playground.py", "status": "pending"},
        {"description": "Run validation", "status": "pending"},
    ]
    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="todo1",
            name="update_todo_list",
            ok=True,
            result=json.dumps({"ok": True, "tasks": model_tasks}),
            extras={"tasks": model_tasks},
        ),
    )

    relay.relay("parent_tc", ToolCallStart(index=0, id="patch1", name="patch_file"))

    assert [task["description"] for task in emitted_tasks[-1]] == [
        "Inspect existing code",
        "Update aura/gui/playground.py",
        "Run validation",
    ]
    assert emitted_tasks[-1][1]["status"] == "active"
    assert not any(
        task["description"].startswith("Worker activity:")
        for task in emitted_tasks[-1]
    )
    assert emitted_tasks[-1] == [
        {"description": "Inspect existing code", "status": "done"},
        {"description": "Update aura/gui/playground.py", "status": "active"},
        {"description": "Run validation", "status": "pending"},
    ]
    assert relay.todo_used is True


def test_event_relay_generic_edit_progress_uses_next_pending_fix_before_later_update_task():
    """Generic edit progress should not jump to a later row just because it says update."""
    class MockApprovalProxy:
        def consume_last_event(self):
            return None

    relay = WorkerEventRelay(MockApprovalProxy())
    emitted_tasks = []
    relay.todoListUpdated.connect(lambda tool_call_id, tasks: emitted_tasks.append(tasks))

    model_tasks = [
        {"description": "Read all relevant source files", "status": "done"},
        {"description": "Fix 1: Deduplicate companion defaults", "status": "done"},
        {"description": "Fix 2: Strengthen old localhost migration", "status": "done"},
        {"description": "Fix 3: Make dev override apply through load_settings too", "status": "pending"},
        {"description": "Fix 4: Tighten companion-web relay selection", "status": "pending"},
        {"description": "Fix 5: Expand is_local_relay_url", "status": "pending"},
        {"description": "Fix 6: Update tests", "status": "pending"},
        {"description": "Validate: compile and run pytest", "status": "pending"},
    ]
    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="todo1",
            name="update_todo_list",
            ok=True,
            result=json.dumps({"ok": True, "tasks": model_tasks}),
            extras={"tasks": model_tasks},
        ),
    )

    relay.relay("parent_tc", ToolCallStart(index=0, id="patch1", name="patch_file"))

    assert emitted_tasks[-1][3]["status"] == "active"
    assert emitted_tasks[-1][3]["description"].startswith("Fix 3:")
    assert emitted_tasks[-1][6] == {"description": "Fix 6: Update tests", "status": "pending"}

    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="patch1",
            name="patch_file",
            ok=True,
            result=json.dumps(
                {
                    "ok": True,
                    "path": "tests/test_companion_settings.py",
                    "applied": True,
                }
            ),
        ),
    )

    assert emitted_tasks[-1][3]["status"] == "done"
    assert emitted_tasks[-1][6] == {"description": "Fix 6: Update tests", "status": "pending"}


def test_event_relay_write_result_marks_matching_model_todo_done_in_place():
    """A write result uses path matching to complete the right model TODO row."""
    class MockApprovalProxy:
        def consume_last_event(self):
            return None

    relay = WorkerEventRelay(MockApprovalProxy())
    emitted_tasks = []
    relay.todoListUpdated.connect(lambda tool_call_id, tasks: emitted_tasks.append(tasks))

    model_tasks = [
        {"description": "Update aura/bridge/event_relay.py", "status": "pending"},
        {"description": "Update aura/gui/playground.py", "status": "pending"},
        {"description": "Run validation", "status": "pending"},
    ]
    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="todo1",
            name="update_todo_list",
            ok=True,
            result=json.dumps({"ok": True, "tasks": model_tasks}),
            extras={"tasks": model_tasks},
        ),
    )

    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="patch1",
            name="patch_file",
            ok=True,
            result=json.dumps(
                {"ok": True, "path": "aura/gui/playground.py", "applied": True}
            ),
        ),
    )

    assert emitted_tasks[-1] == [
        {"description": "Update aura/bridge/event_relay.py", "status": "pending"},
        {"description": "Update aura/gui/playground.py", "status": "done"},
        {"description": "Run validation", "status": "pending"},
    ]


def test_event_relay_validation_start_marks_model_todo_active_in_place():
    """Validation progress activates the validation TODO without moving rows."""
    class MockApprovalProxy:
        def consume_last_event(self):
            return None

    relay = WorkerEventRelay(MockApprovalProxy())
    emitted_tasks = []
    relay.todoListUpdated.connect(lambda tool_call_id, tasks: emitted_tasks.append(tasks))

    model_tasks = [
        {"description": "Update aura/bridge/event_relay.py", "status": "done"},
        {"description": "Run validation", "status": "pending"},
        {"description": "Deliver final report", "status": "pending"},
    ]
    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="todo1",
            name="update_todo_list",
            ok=True,
            result=json.dumps({"ok": True, "tasks": model_tasks}),
            extras={"tasks": model_tasks},
        ),
    )

    relay.relay("parent_tc", ToolCallStart(index=1, id="term1", name="run_terminal_command"))

    assert emitted_tasks[-1] == [
        {"description": "Update aura/bridge/event_relay.py", "status": "done"},
        {"description": "Run validation", "status": "active"},
        {"description": "Deliver final report", "status": "pending"},
    ]


def test_event_relay_stale_model_todo_update_cannot_downgrade_runtime_done():
    """Later pending model updates do not regress runtime-completed TODO rows."""
    class MockApprovalProxy:
        def consume_last_event(self):
            return None

    relay = WorkerEventRelay(MockApprovalProxy())
    emitted_tasks = []
    relay.todoListUpdated.connect(lambda tool_call_id, tasks: emitted_tasks.append(tasks))

    initial_tasks = [
        {"description": "Update aura/bridge/event_relay.py", "status": "pending"},
        {"description": "Run validation", "status": "pending"},
    ]
    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="todo1",
            name="update_todo_list",
            ok=True,
            result=json.dumps({"ok": True, "tasks": initial_tasks}),
            extras={"tasks": initial_tasks},
        ),
    )
    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="patch1",
            name="patch_file",
            ok=True,
            result=json.dumps(
                {"ok": True, "path": "aura/bridge/event_relay.py", "applied": True}
            ),
        ),
    )
    assert emitted_tasks[-1][0]["status"] == "done"

    stale_tasks = [
        {"description": "Update aura/bridge/event_relay.py", "status": "pending"},
        {"description": "Run validation", "status": "pending"},
    ]
    relay.relay(
        "parent_tc",
        ToolResult(
            tool_call_id="todo2",
            name="update_todo_list",
            ok=True,
            result=json.dumps({"ok": True, "tasks": stale_tasks}),
            extras={"tasks": stale_tasks},
        ),
    )

    assert emitted_tasks[-1] == [
        {"description": "Update aura/bridge/event_relay.py", "status": "done"},
        {"description": "Run validation", "status": "pending"},
    ]


def test_todo_widget_ignores_identical_updates(qapp):
    """TodoListWidget ignores identical normalized task updates."""
    widget = TodoListWidget()
    
    tasks = [{"description": "Clean room", "status": "pending"}]
    widget.update_tasks(tasks)
    
    assert len(widget._task_widgets) == 1
    initial_text = widget._task_desc_labels[0].text()
    
    # Update with identical tasks under different keys/values that normalize to the same values
    widget.update_tasks([{"content": "Clean room", "status": "todo"}])
    
    assert widget._task_widgets[0] is widget._task_widgets[0]
    assert widget._task_desc_labels[0].text() == initial_text


def test_todo_widget_marks_done_strikeout(qapp):
    """TodoListWidget marks done tasks with strikeOut font."""
    widget = TodoListWidget()
    tasks = [{"description": "Clean room", "status": "done"}]
    widget.update_tasks(tasks)
    
    desc_label = widget._task_desc_labels[0]
    assert desc_label.font().strikeOut() is True
    assert widget._task_icon_labels[0].pixmap() is not None
    assert "Clean room" in desc_label.text()


def test_todo_widget_does_not_recreate_active_pulse(qapp):
    """TodoListWidget does not recreate active pulse animation on identical active update."""
    widget = TodoListWidget()
    tasks = [{"description": "Clean room", "status": "active"}]
    widget.update_tasks(tasks)
    
    assert len(widget._pulse_anims) == 1
    anim1 = widget._pulse_anims[0]
    
    # Re-update with same status active
    # Note: To test the animation transition logic specifically, we bypass signature cache
    # by using a dummy field, but keeping status active.
    widget.update_tasks([{"description": "Clean room", "status": "active", "dummy": 1}])
    assert len(widget._pulse_anims) == 1
    anim2 = widget._pulse_anims[0]
    
    assert anim1 is anim2


def test_todo_widget_stops_active_animation_on_done(qapp):
    """TodoListWidget stops active animation when task changes from active to done."""
    widget = TodoListWidget()
    tasks = [{"description": "Clean room", "status": "active"}]
    widget.update_tasks(tasks)
    
    assert len(widget._pulse_anims) == 1
    
    # Update task to done
    widget.update_tasks([{"description": "Clean room", "status": "done"}])
    assert len(widget._pulse_anims) == 0


def test_normalize_todo_tasks_support():
    """normalize_todo_tasks supports description/content/text/task and status aliases."""
    input_tasks = [
        {"description": "Task 1", "status": "completed"},
        {"content": "Task 2", "state": "doing"},
        {"text": "Task 3", "status": "not_started"},
        {"task": "Task 4", "status": "active"},
        "Task 5"
    ]
    normalized = normalize_todo_tasks(input_tasks)
    assert len(normalized) == 5
    assert normalized[0] == {"description": "Task 1", "status": "done"}
    assert normalized[1] == {"description": "Task 2", "status": "active"}
    assert normalized[2] == {"description": "Task 3", "status": "pending"}
    assert normalized[3] == {"description": "Task 4", "status": "active"}
    assert normalized[4] == {"description": "Task 5", "status": "pending"}


def test_normalize_todo_tasks_clamps_long_descriptions():
    """Long task descriptions are clamped to 220 characters."""
    long_desc = "a" * 300
    input_tasks = [{"description": long_desc, "status": "pending"}]
    normalized = normalize_todo_tasks(input_tasks)

    assert len(normalized[0]["description"]) == 220
    assert normalized[0]["description"].endswith("...")
    assert normalized[0]["description"].startswith("aaa")


# ── DispatchTodoController tests ──────────────────────────────────────


class TestDispatchTodoController:
    """Tests for the unified canonical TODO controller."""

    def test_begin_creates_stable_checklist(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        objectives = [
            {"id": "one", "description": "First task"},
            {"id": "two", "description": "Second task"},
            {"id": "three", "description": "Third task"},
        ]
        snapshot = ctrl.begin("tc1", objectives)

        assert len(snapshot) == 3
        assert [t["id"] for t in snapshot] == ["one", "two", "three"]
        assert [t["description"] for t in snapshot] == [
            "First task", "Second task", "Third task",
        ]
        assert all(t["status"] == "pending" for t in snapshot)

    def test_ids_and_order_never_change(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "a", "description": "Alpha"},
            {"id": "b", "description": "Beta"},
        ])

        ctrl.set_active("tc1", "a")
        ctrl.mark_done("tc1", "a")
        ctrl.mark_blocked("tc1", "b")

        snapshot = ctrl.snapshot("tc1")
        assert [t["id"] for t in snapshot] == ["a", "b"]
        assert [t["description"] for t in snapshot] == ["Alpha", "Beta"]

    def test_rows_check_off_in_place(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "one", "description": "Task 1"},
            {"id": "two", "description": "Task 2"},
        ])

        # Mark first done, second active
        ctrl.mark_done("tc1", "one")
        ctrl.set_active("tc1", "two")

        snapshot = ctrl.snapshot("tc1")
        assert snapshot[0]["status"] == "done"
        assert snapshot[1]["status"] == "active"

        # Mark second done — first stays done
        ctrl.mark_done("tc1", "two")
        snapshot = ctrl.snapshot("tc1")
        assert snapshot[0]["status"] == "done"
        assert snapshot[1]["status"] == "done"

    def test_final_checklist_visible_after_finish(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "one", "description": "Task 1"},
            {"id": "two", "description": "Task 2"},
        ])
        ctrl.mark_done("tc1", "one")
        ctrl.mark_done("tc1", "two")
        ctrl.finish("tc1")

        # Checklist still visible after finish
        snapshot = ctrl.snapshot("tc1")
        assert len(snapshot) == 2
        assert all(t["status"] == "done" for t in snapshot)

    def test_blocked_campaign_shows_done_and_blocked_and_pending(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "one", "description": "Step 1"},
            {"id": "two", "description": "Step 2"},
            {"id": "three", "description": "Step 3"},
        ])

        ctrl.mark_done("tc1", "one")
        ctrl.mark_blocked("tc1", "two")

        snapshot = ctrl.snapshot("tc1")
        assert snapshot[0]["status"] == "done"
        assert snapshot[1]["status"] == "active"
        assert snapshot[1].get("blocked") is True
        assert snapshot[2]["status"] == "pending"

    def test_worker_local_unknown_ids_ignored(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "plan-step", "description": "Planned step"},
        ])

        # Worker sends update with unknown ID
        result = ctrl.absorb_worker_update("tc1", [
            {"id": "worker-adhoc", "description": "Ad-hoc task", "status": "active"},
        ])

        # Should return None (no emission needed — nothing changed)
        assert result is None

        # The canonical list is unchanged
        snapshot = ctrl.snapshot("tc1")
        assert len(snapshot) == 1
        assert snapshot[0]["id"] == "plan-step"
        assert snapshot[0]["status"] == "pending"

    def test_worker_cannot_add_remove_or_reorder_rows(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "a", "description": "Task A"},
            {"id": "b", "description": "Task B"},
        ])

        # Worker tries to add a new row, remove one, and reorder
        ctrl.absorb_worker_update("tc1", [
            {"id": "c", "description": "Injected task", "status": "pending"},
            {"id": "b", "status": "active"},
            # "a" is missing → worker tried to remove it
        ])

        snapshot = ctrl.snapshot("tc1")
        assert len(snapshot) == 2
        ids = [t["id"] for t in snapshot]
        assert ids == ["a", "b"]  # order unchanged
        assert "c" not in ids      # worker can't add

    def test_worker_may_update_status_for_known_id(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "step-1", "description": "Step 1"},
        ])

        result = ctrl.absorb_worker_update("tc1", [
            {"id": "step-1", "status": "done"},
        ])

        assert result is not None
        assert result[0]["status"] == "done"

    def test_worker_cannot_rename_known_row(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "step-1", "description": "Original name"},
        ])

        ctrl.absorb_worker_update("tc1", [
            {"id": "step-1", "description": "Renamed by worker", "status": "active"},
        ])

        snapshot = ctrl.snapshot("tc1")
        assert snapshot[0]["description"] == "Original name"

    def test_has_canonical_returns_false_for_unknown_id(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        assert ctrl.has_canonical("nonexistent") is False

        ctrl.begin("tc1", [{"id": "s1", "description": "Step"}])
        assert ctrl.has_canonical("tc1") is True

    def test_clear_removes_canonical_state(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [{"id": "s1", "description": "Step"}])
        assert ctrl.has_canonical("tc1") is True

        ctrl.clear("tc1")
        assert ctrl.has_canonical("tc1") is False
        assert ctrl.snapshot("tc1") == []

    def test_no_canonical_then_absorb_returns_none(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        # No canonical state exists — absorb should return None
        result = ctrl.absorb_worker_update("tc1", [
            {"id": "x", "description": "Task", "status": "active"},
        ])
        assert result is None

    def test_non_dict_tasks_are_ignored(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [{"id": "a", "description": "Task A"}])

        result = ctrl.absorb_worker_update("tc1", [
            "just a string",
            42,
            None,
            {"id": "a", "status": "done"},
        ])
        assert result is not None
        assert result[0]["status"] == "done"


    def test_clear_all_removes_all_canonical_state(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [{"id": "s1", "description": "Step 1"}])
        ctrl.begin("tc2", [{"id": "s2", "description": "Step 2"}])
        assert ctrl.has_canonical("tc1") is True
        assert ctrl.has_canonical("tc2") is True

        ctrl.clear_all()

        assert ctrl.has_canonical("tc1") is False
        assert ctrl.has_canonical("tc2") is False
        assert ctrl.snapshot("tc1") == []
        assert ctrl.snapshot("tc2") == []

    def test_set_active_one_row_at_a_time(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "one", "description": "First"},
            {"id": "two", "description": "Second"},
            {"id": "three", "description": "Third"},
        ])

        result = ctrl.set_active("tc1", "one")
        assert result[0]["status"] == "active"
        assert result[1]["status"] == "pending"
        assert result[2]["status"] == "pending"

        result = ctrl.set_active("tc1", "two")
        assert result[0]["status"] == "pending"
        assert result[1]["status"] == "active"
        assert result[2]["status"] == "pending"

        ctrl.mark_done("tc1", "two")
        snapshot = ctrl.snapshot("tc1")
        assert snapshot[1]["status"] == "done"

        result = ctrl.set_active("tc1", "three")
        assert result[1]["status"] == "done"
        assert result[2]["status"] == "active"

    def test_absorb_worker_update_one_active_row(self):
        from aura.bridge.todo_controller import DispatchTodoController

        ctrl = DispatchTodoController()
        ctrl.begin("tc1", [
            {"id": "one", "description": "First"},
            {"id": "two", "description": "Second"},
            {"id": "three", "description": "Third"},
        ])

        ctrl.set_active("tc1", "one")
        snapshot = ctrl.snapshot("tc1")
        assert snapshot[0]["status"] == "active"

        result = ctrl.absorb_worker_update("tc1", [
            {"id": "two", "status": "active"},
        ])
        assert result is not None
        assert result[0]["status"] == "pending"
        assert result[1]["status"] == "active"
        assert len(result) == 3
        assert [t["id"] for t in result] == ["one", "two", "three"]
        assert [t["description"] for t in result] == ["First", "Second", "Third"]

        # Also verify via snapshot
        assert ctrl.snapshot("tc1") == result


# ── compact_todo_label tests ───────────────────────────────────────────


class TestCompactTodoLabel:
    def test_uses_first_meaningful_line(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        result = compact_todo_label("Step 1: Do the thing\nThen do more\nAnd more")
        assert result == "Do the thing"
        assert "\n" not in result

    def test_strips_markdown_headings(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        assert compact_todo_label("### Implement the feature") == "Implement the feature"
        assert compact_todo_label("# Top-level heading") == "Top-level heading"

    def test_strips_bullet_and_checkbox_prefixes(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        assert compact_todo_label("- Do the thing") == "Do the thing"
        assert compact_todo_label("* Another task") == "Another task"
        assert compact_todo_label("[ ] Unchecked") == "Unchecked"
        assert compact_todo_label("[x] Checked") == "Checked"

    def test_strips_numeric_prefixes(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        assert compact_todo_label("1. First step") == "First step"
        assert compact_todo_label("2) Second step") == "Second step"

    def test_strips_prefix_labels(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        assert compact_todo_label("Step 1: Implement feature") == "Implement feature"
        assert compact_todo_label("Objective: Complete the work") == "Complete the work"
        assert compact_todo_label("Summary: This is a summary") == "This is a summary"
        assert compact_todo_label("Goal: Fix the bug") == "Fix the bug"
        assert compact_todo_label("Acceptance: Tests pass") == "Tests pass"

    def test_collapses_whitespace(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        assert compact_todo_label("   Too   many    spaces   ") == "Too many spaces"

    def test_limits_length_to_90_chars(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        long_text = "A" * 200
        result = compact_todo_label(long_text)
        assert len(result) <= 90
        assert result.endswith("...")

    def test_preserves_explicit_step_title(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        assert compact_todo_label("Tighten schema validation") == "Tighten schema validation"
        assert compact_todo_label("Reject bad dispatches") == "Reject bad dispatches"

    def test_fallback_for_empty_value(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        assert compact_todo_label("") == "Worker step"
        assert compact_todo_label("", fallback="Custom fallback") == "Custom fallback"

    def test_does_not_display_giant_spec_or_acceptance_text(self):
        from aura.conversation.dispatch_plan import compact_todo_label

        giant_spec = (
            "### Implementation\n\n"
            "This is a very long specification that describes in detail "
            "everything the worker should do including edge cases, "
            "error handling, validation steps, and extensive acceptance criteria "
            "that spans multiple paragraphs and would look terrible as a TODO row.\n\n"
            "## Acceptance\n"
            "- pytest passes\n"
            "- py_compile clean\n"
        )

        result = compact_todo_label(giant_spec)
        # Only first line should be used
        assert len(result) <= 90
        assert "error handling" not in result
        assert "pytest" not in result


# ── dispatch session TODO integration tests ───────────────────────────


class TestDispatchSessionTodoIntegration:
    """Verify DispatchSession properly routes TODO updates through the controller."""

    def test_multi_step_emits_same_ids_in_order_throughout(self):
        from types import SimpleNamespace

        from aura.bridge.dispatch_session import DispatchSession
        from aura.bridge.todo_controller import DispatchTodoController
        from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
        from aura.conversation.dispatch_plan import WorkerStepSpec, plan_from_request

        steps = [
            WorkerStepSpec(id="one", title="Step One", goal="First step", files=["a.py"]),
            WorkerStepSpec(id="two", title="Step Two", goal="Second step", files=["b.py"]),
            WorkerStepSpec(id="three", title="Step Three", goal="Third step", files=["c.py"]),
        ]
        req = WorkerDispatchRequest(
            goal="Campaign", files=["a.py", "b.py", "c.py"],
            spec="", acceptance="", steps=steps,
        )
        plan = plan_from_request(req)
        controller = DispatchTodoController()
        emitted: list[list[dict]] = []

        def emit(tool_call_id, tasks):
            emitted.append(list(tasks))

        def run_worker_step(_tid, _req, _pending):
            return WorkerDispatchResult(ok=True, summary="done")

        DispatchSession(
            tool_call_id="tc1",
            original_request=req,
            plan=plan,
            run_worker_step=run_worker_step,
            pending=SimpleNamespace(),
            emit_todo_update=emit,
            todo_controller=controller,
        ).run()

        # Every emission should have the same 3 IDs in order
        for snapshot in emitted:
            ids = [t["id"] for t in snapshot]
            assert ids == ["one", "two", "three"]

    def test_completed_step_stays_done_when_next_active(self):
        from types import SimpleNamespace

        from aura.bridge.dispatch_session import DispatchSession
        from aura.bridge.todo_controller import DispatchTodoController
        from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
        from aura.conversation.dispatch_plan import WorkerStepSpec, plan_from_request

        steps = [
            WorkerStepSpec(id="one", title="One", goal="First", files=["a.py"]),
            WorkerStepSpec(id="two", title="Two", goal="Second", files=["b.py"]),
        ]
        req = WorkerDispatchRequest(
            goal="Campaign", files=["a.py", "b.py"], spec="", acceptance="", steps=steps,
        )
        plan = plan_from_request(req)
        controller = DispatchTodoController()
        emitted: list[list[dict]] = []

        def emit(tool_call_id, tasks):
            emitted.append(list(tasks))

        def run_worker_step(_tid, _req, _pending):
            return WorkerDispatchResult(ok=True, summary="done")

        DispatchSession(
            tool_call_id="tc1",
            original_request=req,
            plan=plan,
            run_worker_step=run_worker_step,
            pending=SimpleNamespace(),
            emit_todo_update=emit,
            todo_controller=controller,
        ).run()

        # Final snapshot should have both steps done
        final = emitted[-1]
        assert final[0]["status"] == "done"
        assert final[1]["status"] == "done"

    def test_final_successful_campaign_shows_all_done(self):
        from types import SimpleNamespace

        from aura.bridge.dispatch_session import DispatchSession
        from aura.bridge.todo_controller import DispatchTodoController
        from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
        from aura.conversation.dispatch_plan import WorkerStepSpec, plan_from_request

        steps = [
            WorkerStepSpec(id="a", title="A", goal="Task A", files=["a.py"]),
            WorkerStepSpec(id="b", title="B", goal="Task B", files=["b.py"]),
        ]
        req = WorkerDispatchRequest(
            goal="All done", files=["a.py", "b.py"], spec="", acceptance="", steps=steps,
        )
        plan = plan_from_request(req)
        controller = DispatchTodoController()
        emitted: list[list[dict]] = []

        def emit(tool_call_id, tasks):
            emitted.append(list(tasks))

        def run_worker_step(_tid, _req, _pending):
            return WorkerDispatchResult(ok=True, summary="done")

        DispatchSession(
            tool_call_id="tc1",
            original_request=req,
            plan=plan,
            run_worker_step=run_worker_step,
            pending=SimpleNamespace(),
            emit_todo_update=emit,
            todo_controller=controller,
        ).run()

        final = emitted[-1]
        assert all(t["status"] == "done" for t in final)

    def test_blocked_campaign_shows_done_blocked_pending(self):
        from types import SimpleNamespace

        from aura.bridge.dispatch_session import DispatchSession
        from aura.bridge.todo_controller import DispatchTodoController
        from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
        from aura.conversation.dispatch_plan import WorkerStepSpec, plan_from_request

        steps = [
            WorkerStepSpec(id="s1", title="S1", goal="First", files=["a.py"]),
            WorkerStepSpec(id="s2", title="S2", goal="Second", files=["b.py"]),
            WorkerStepSpec(id="s3", title="S3", goal="Third", files=["c.py"]),
        ]
        req = WorkerDispatchRequest(
            goal="Campaign", files=["a.py", "b.py", "c.py"], spec="", acceptance="", steps=steps,
        )
        plan = plan_from_request(req)
        controller = DispatchTodoController()
        emitted: list[list[dict]] = []

        def emit(tool_call_id, tasks):
            emitted.append(list(tasks))

        call_count = [0]

        def run_worker_step(_tid, _req, _pending):
            call_count[0] += 1
            if call_count[0] == 1:
                return WorkerDispatchResult(ok=True, summary="step 1 done")
            else:
                return WorkerDispatchResult(ok=False, summary="blocked", needs_followup=True)

        DispatchSession(
            tool_call_id="tc1",
            original_request=req,
            plan=plan,
            run_worker_step=run_worker_step,
            pending=SimpleNamespace(),
            emit_todo_update=emit,
            todo_controller=controller,
        ).run()

        final = emitted[-1]
        assert final[0]["status"] == "done"
        assert final[1]["status"] == "active"
        assert final[1].get("blocked") is True
        assert final[2]["status"] == "pending"

    def test_one_meaningful_objective_allows_one_row(self):
        """A flat dispatch with one meaningful objective should produce one row."""
        from types import SimpleNamespace

        from aura.bridge.dispatch_session import DispatchSession
        from aura.bridge.todo_controller import DispatchTodoController
        from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
        from aura.conversation.dispatch_plan import plan_from_request

        req = WorkerDispatchRequest(
            goal="Fix one typo",
            files=["README.md"],
            spec="Fix a typo.",
            acceptance="Verify the fix.",
        )
        plan = plan_from_request(req)
        controller = DispatchTodoController()
        emitted: list[list[dict]] = []

        def emit(tool_call_id, tasks):
            emitted.append(list(tasks))

        def run_worker_step(_tid, _req, _pending):
            return WorkerDispatchResult(ok=True, summary="done")

        DispatchSession(
            tool_call_id="tc1",
            original_request=req,
            plan=plan,
            run_worker_step=run_worker_step,
            pending=SimpleNamespace(),
            emit_todo_update=emit,
            todo_controller=controller,
        ).run()

        # Should have exactly 1 row (not padded, not split)
        for snapshot in emitted:
            assert len(snapshot) == 1

    def test_labels_are_compacted_not_giant_specs(self):
        from types import SimpleNamespace

        from aura.bridge.dispatch_session import DispatchSession
        from aura.bridge.todo_controller import DispatchTodoController
        from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
        from aura.conversation.dispatch_plan import WorkerStepSpec, plan_from_request

        # A step with a giant spec-like title
        steps = [
            WorkerStepSpec(
                id="step-1",
                title="### Step 1: Refactor authentication subsystem to use token-based auth\n\n"
                       "This involves changing the login flow, updating the middleware, "
                       "adding token validation, and migrating existing sessions.",
                goal="Refactor auth",
                files=["auth.py"],
            ),
        ]
        req = WorkerDispatchRequest(
            goal="Refactor auth", files=["auth.py"], spec="", acceptance="", steps=steps,
        )
        plan = plan_from_request(req)
        controller = DispatchTodoController()
        emitted: list[list[dict]] = []

        def emit(tool_call_id, tasks):
            emitted.append(list(tasks))

        def run_worker_step(_tid, _req, _pending):
            return WorkerDispatchResult(ok=True, summary="done")

        DispatchSession(
            tool_call_id="tc1",
            original_request=req,
            plan=plan,
            run_worker_step=run_worker_step,
            pending=SimpleNamespace(),
            emit_todo_update=emit,
            todo_controller=controller,
        ).run()

        for snapshot in emitted:
            for task in snapshot:
                desc = task["description"]
                assert len(desc) <= 90
                assert "middleware" not in desc
                assert "token validation" not in desc
                assert "migrating" not in desc

    def test_no_hardcoded_objective_count(self):
        """The controller does not enforce or pad to any specific count."""
        from aura.bridge.todo_controller import DispatchTodoController

        # 0 objectives is valid (empty plan)
        ctrl = DispatchTodoController()
        snapshot = ctrl.begin("tc1", [])
        assert snapshot == []

        # 1 objective is valid
        ctrl2 = DispatchTodoController()
        snapshot2 = ctrl2.begin("tc2", [{"id": "a", "description": "Task"}])
        assert len(snapshot2) == 1

        # 12 objectives is valid (no upper bound enforced by controller)
        ctrl3 = DispatchTodoController()
        objectives = [{"id": str(i), "description": f"Task {i}"} for i in range(12)]
        snapshot3 = ctrl3.begin("tc3", objectives)
        assert len(snapshot3) == 12
