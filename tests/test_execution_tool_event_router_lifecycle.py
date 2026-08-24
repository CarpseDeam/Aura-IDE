"""ExecutionToolEventRouter forwarding for the file-edit lifecycle.

Pure-Python: the router is a thin one-line forwarder, so a fake playground
object is enough to prove wiring without constructing real Qt widgets.
"""
from __future__ import annotations

from aura.gui.execution_tool_event_router import ExecutionToolEventRouter


class _FakePlayground:
    def __init__(self) -> None:
        self.lifecycle_calls: list[tuple] = []

    def handle_file_edit_lifecycle(self, tool_call_id, tool_name, phase, changes, reason):
        self.lifecycle_calls.append((tool_call_id, tool_name, phase, changes, reason))


def test_file_edit_lifecycle_forwards_to_playground() -> None:
    playground = _FakePlayground()
    router = ExecutionToolEventRouter(playground=playground)

    changes = [{"change_id": "call-1:0", "path": "a.py", "action": "modify"}]
    router.on_execution_file_edit_lifecycle(
        "run-1", "call-1", "write_file", "applied", changes, ""
    )

    assert playground.lifecycle_calls == [
        ("call-1", "write_file", "applied", changes, "")
    ]


def test_playground_no_longer_exposes_show_code_diff() -> None:
    """The real AuraPlayground must not retain the editor-driving diff path."""
    from aura.gui.playground import AuraPlayground

    assert not hasattr(AuraPlayground, "show_code_diff")


def test_router_no_longer_exposes_execution_diff_decided() -> None:
    """Phase 4B: execution diff decisions no longer render in the Progress pane.

    Authoritative applied writes already belong to FileEditProjection and the
    code editor, so the router has no forwarding method left for this event.
    """
    assert not hasattr(ExecutionToolEventRouter, "on_execution_diff_decided")


def test_playground_no_longer_exposes_add_diff_card() -> None:
    from aura.gui.playground import AuraPlayground

    assert not hasattr(AuraPlayground, "add_diff_card")


def test_router_neither_requires_nor_owns_a_chat_view() -> None:
    """Every remaining route targets the playground, so ChatView is dead here."""
    import inspect

    params = inspect.signature(ExecutionToolEventRouter.__init__).parameters
    assert list(params) == ["self", "playground"]

    router = ExecutionToolEventRouter(playground=_FakePlayground())
    assert not hasattr(router, "_chat")

    import aura.gui.execution_tool_event_router as router_module

    assert "ChatView" not in inspect.getsource(router_module)
