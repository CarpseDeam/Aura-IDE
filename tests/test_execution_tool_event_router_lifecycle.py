"""ExecutionToolEventRouter forwarding for the file-edit lifecycle.

Pure-Python: the router is a thin one-line forwarder, so a fake playground
object is enough to prove wiring without constructing real Qt widgets.
"""
from __future__ import annotations

from aura.gui.execution_tool_event_router import ExecutionToolEventRouter


class _FakePlayground:
    def __init__(self) -> None:
        self.lifecycle_calls: list[tuple] = []
        self.diff_card_calls: list[tuple] = []

    def handle_file_edit_lifecycle(self, tool_call_id, tool_name, phase, changes, reason):
        self.lifecycle_calls.append((tool_call_id, tool_name, phase, changes, reason))

    def add_diff_card(self, execution_tool_id, rel_path, old, new, decision, is_new_file):
        self.diff_card_calls.append(
            (execution_tool_id, rel_path, old, new, decision, is_new_file)
        )


def test_file_edit_lifecycle_forwards_to_playground() -> None:
    playground = _FakePlayground()
    router = ExecutionToolEventRouter(playground=playground, chat=None)

    changes = [{"change_id": "call-1:0", "path": "a.py", "action": "modify"}]
    router.on_execution_file_edit_lifecycle(
        "run-1", "call-1", "write_file", "applied", changes, ""
    )

    assert playground.lifecycle_calls == [
        ("call-1", "write_file", "applied", changes, "")
    ]


def test_diff_decided_routes_to_diff_card_not_editor() -> None:
    """The approval-derived diff decision must never claim editor authority.

    It is recorded as a diff card (history/record), and the router exposes
    no method that lets a diff decision touch the workspace editor tabs.
    """
    playground = _FakePlayground()
    router = ExecutionToolEventRouter(playground=playground, chat=None)

    router.on_execution_diff_decided(
        "run-1", "call-1", "approve", "a.py", "old", "new", False
    )

    assert playground.diff_card_calls == [
        ("call-1", "a.py", "old", "new", "approve", False)
    ]


def test_playground_no_longer_exposes_show_code_diff() -> None:
    """The real AuraPlayground must not retain the editor-driving diff path."""
    from aura.gui.playground import AuraPlayground

    assert not hasattr(AuraPlayground, "show_code_diff")
