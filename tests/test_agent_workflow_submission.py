"""One submission-time Agent context stays attached to the queued turn."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtCore")

from aura.agents.graph_models import WorkflowGraph  # noqa: E402
from aura.agents.identity import AgentScope  # noqa: E402
from aura.agents.turn_context import AgentTurnContext  # noqa: E402
from aura.agents.workflow_plan import WorkflowRunPlan  # noqa: E402
from aura.gui.input_panel import SendPayload  # noqa: E402
from aura.gui.send_handler import SendHandler  # noqa: E402


class _History:
    def append_user_text(self, *args, **kwargs) -> None:
        pass

    def append_user_multimodal(self, *args, **kwargs) -> None:
        pass


class _Bridge:
    def __init__(self) -> None:
        self.history = _History()
        self.running = True
        self.agent_contexts: list[AgentTurnContext] = []
        self.sends: list[dict] = []

    def is_running(self) -> bool:
        return self.running

    def set_submitted_agent_context(self, context: AgentTurnContext) -> None:
        self.agent_contexts.append(context)

    def authorize_external_reads(self, paths):
        return tuple(paths or ())

    def set_turn_target_files(self, paths) -> None:
        pass

    def send(self, **kwargs) -> None:
        self.sends.append(kwargs)


class _Chat:
    def add_user(self, *args, **kwargs) -> None:
        pass

    def add_error(self, *args, **kwargs) -> None:
        raise AssertionError(args)

    def scroll_to_bottom(self, *args, **kwargs) -> None:
        pass

    def begin_assistant(self) -> None:
        pass


class _Input:
    def __init__(self) -> None:
        self.queued: list[int] = []

    def set_queued_messages(self, count: int) -> None:
        self.queued.append(count)


def _plan(graph_id: str, name: str) -> WorkflowRunPlan:
    graph = WorkflowGraph(graph_id=graph_id, scope=AgentScope.PROJECT, name=name)
    return WorkflowRunPlan(
        graph_id=graph_id,
        scope=AgentScope.PROJECT,
        name=name,
        description="",
        provider="deepseek",
        graph=graph,
    )


def test_a_queued_turn_keeps_its_submitted_workflow_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: True,
    )
    first = _plan("workflowone1", "First")
    later = _plan("workflowtwo2", "Later")
    selected = {"context": AgentTurnContext.active_workflow(first)}
    bridge = _Bridge()
    input_panel = _Input()
    handler = SendHandler(
        bridge=bridge,
        chat=_Chat(),
        input_panel=input_panel,
        settings=SimpleNamespace(provider="deepseek"),
        workspace_root=tmp_path,
        agent_context_provider=lambda **_kwargs: selected["context"],
    )

    assert handler.handle_send(SendPayload("queued task", []), "model", "off") is False
    selected["context"] = AgentTurnContext.active_workflow(later)
    bridge.running = False
    handler.process_message_queue("ignored-model", "off")

    assert bridge.agent_contexts == [AgentTurnContext.active_workflow(first)]
    assert bridge.sends == [{"model": "model", "thinking": "off"}]
    assert input_panel.queued == [1, 0]


def test_an_off_gate_deposits_off_context_so_no_earlier_plan_can_carry_over(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: True,
    )
    bridge = _Bridge()
    bridge.running = False
    handler = SendHandler(
        bridge=bridge,
        chat=_Chat(),
        input_panel=_Input(),
        settings=SimpleNamespace(provider="deepseek"),
        workspace_root=tmp_path,
        agent_context_provider=lambda **_kwargs: AgentTurnContext.off(),
    )

    assert handler.handle_send(SendPayload("ordinary task", []), "model", "off") is True

    assert bridge.agent_contexts == [AgentTurnContext.off()]
