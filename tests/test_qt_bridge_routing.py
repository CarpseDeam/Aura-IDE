"""Route handoff contract: ConversationBridge consumes the TaskRoute selected
at send time as the authority for production turn-context preparation.

The route is never recomputed inside the bridge; ``_prepare_turn_context``
projects the supplied lane onto the task kind that context composition and the
production manager receive. Callers that supply no route keep the previous
research-only fallback.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.bridge.qt_bridge import ConversationBridge  # noqa: E402
from aura.conversation.task_router import TaskLane, TaskRoute  # noqa: E402


@pytest.fixture
def bridge(monkeypatch, tmp_path) -> ConversationBridge:
    """A real bridge with the context system stubbed so the unit stays hermetic.

    ``compose_system_prompt`` captures the task kind the turn preparation feeds
    into production context composition; the workspace is a scratch dir.
    """
    captured: dict = {}

    def _compose(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            system_prompt="system",
            context_text="context",
            ledger=[],
        )

    monkeypatch.setattr("aura.bridge.qt_bridge.compose_system_prompt", _compose)
    monkeypatch.setattr(
        "aura.bridge.qt_bridge.context_gearbox_metadata",
        lambda *args, **kwargs: {"summary": {}, "ledger": []},
    )
    app = QApplication.instance() or QApplication([])
    assert app is not None
    b = ConversationBridge(parent_widget=None, provider="test")
    b.set_workspace_root(tmp_path)
    b._captured = captured
    return b


def _prepare(bridge: ConversationBridge, route: TaskRoute | None, text: str) -> None:
    bridge._history.append_user_text(text)
    bridge._turn_task_route = route
    bridge._prepare_turn_context()


def test_implementation_route_reaches_context_as_implementation(bridge):
    route = TaskRoute(TaskLane.implementation, "implementation", 0.85, "test")
    _prepare(bridge, route, "Fix the queue draining bug in send_handler.py")

    assert bridge._turn_task_kind == "implementation"
    assert bridge._captured["task_kind"] == "implementation"


def test_validation_route_reaches_context_as_validation(bridge):
    route = TaskRoute(TaskLane.validation, "validation", 0.9, "test")
    _prepare(bridge, route, "run pytest tests/test_queue_behavior.py")

    assert bridge._turn_task_kind == "validation"
    assert bridge._captured["task_kind"] == "validation"


def test_research_route_keeps_its_research_action(bridge):
    route = TaskRoute(TaskLane.research, "web_research", 1.0, "test")
    _prepare(bridge, route, "Are there any World Cup matches today?")

    assert bridge._turn_task_kind == "web_research"
    assert bridge._captured["task_kind"] == "web_research"

    hybrid = TaskRoute(TaskLane.research, "research_then_worker", 0.9, "test")
    bridge._turn_task_route = hybrid
    bridge._prepare_turn_context()
    assert bridge._turn_task_kind == "research_then_worker"


def test_chat_route_stays_chat(bridge):
    route = TaskRoute(TaskLane.chat, "chat", 0.6, "test")
    _prepare(bridge, route, "hello there")

    # Chat has no production task kind, matching pre-route behavior.
    assert bridge._turn_task_kind is None
    assert bridge._captured["task_kind"] is None


def test_no_route_falls_back_to_research_only_recomputation(bridge):
    # Callers that genuinely do not supply a route keep the old behavior:
    # research text still maps to its research task kind...
    bridge._history.append_user_text("Are there any World Cup matches today?")
    bridge._turn_task_route = None
    bridge._prepare_turn_context()
    assert bridge._turn_task_kind == "answer_only"

    # ...and ordinary coding text stays task_kind None (no implementation
    # inference is invented inside the bridge).
    bridge._history.append_user_text("Fix the queue draining bug in send_handler.py")
    bridge._prepare_turn_context()
    assert bridge._turn_task_kind is None
