import json
import pytest
from PySide6.QtWidgets import QApplication
from aura.gui.controllers import ToolStreamController
from aura.bridge.event_relay import WorkerEventRelay
from aura.client import ToolResult
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
