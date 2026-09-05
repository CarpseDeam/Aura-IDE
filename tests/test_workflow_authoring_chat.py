"""Native saved Workflow cards through the actual root Qt bridge."""

import json
import threading
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QApplication, QInputDialog
from test_workflow_authoring import authoring_setup, editable_spec, review_spec

from aura.agents.graph_local_state import WorkflowLocalState
from aura.agents.turn_context import AgentTurnMode
from aura.bridge.qt_bridge import ConversationBridge
from aura.client import ContentDelta, Done, ToolCallArgsDelta, ToolCallEnd, ToolCallStart
from aura.gui.chat_view import ChatView
from aura.gui.main_window_agents import MainWindowAgentsController
from aura.gui.widgets.aura_glow import AuraPhaseDriver
from aura.gui.workflow_chat_controller import WorkflowChatController
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class BridgeSignals(QObject):
    workflowAuthored = Signal(object)
    started = Signal()
    finished = Signal()
    requested_read_only = False
    running = False

    def is_running(self):
        return self.running


def test_card_refinement_undo_reopen_and_run_target_exact_saved_graph(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("aura.config.has_usable_provider_configuration", lambda _: True)
    service, _ = authoring_setup(tmp_path)
    owner = MainWindowAgentsController(
        SimpleNamespace(_edge_rail=SimpleNamespace(agents_tab=None)),
        workspace_root=tmp_path / "project",
        store_factory=lambda _: service.agents,
        state_factory=lambda _: service.local_state,
        graph_store_factory=lambda _: service.workflows,
        workflow_state_factory=lambda root: WorkflowLocalState(root, state_root=tmp_path / "state"),
        model_context=lambda: ("deepseek", "deepseek-chat", "off"),
    )
    service = owner.capture_workflow_authoring()
    bridge = BridgeSignals()
    driver = AuraPhaseDriver(qapp)
    chat = ChatView(driver)
    runs = []
    controller = WorkflowChatController(
        bridge=bridge, chat=chat, owner=owner, submit_run=lambda *args: runs.append(args)
    )
    first = service.create(review_spec())
    bridge.workflowAuthored.emit(first)
    card = controller.cards[0]
    chat.assistant_done()
    chat.begin_assistant()
    updated = service.update(
        first.document.graph.graph_id,
        first.document.revision,
        replace(editable_spec(first.document), name="Careful review"),
    )
    bridge.workflowAuthored.emit(updated)
    assert controller.cards == (card,)
    assert card.saved.document == updated.document
    assert not card.undo_button.isHidden()
    assert "Aura's current model" in card.details.text()
    card.undo_button.click()
    assert card.saved.document == first.document
    card.open_button.click()
    assert owner.graphs.current_graph == first.document.graph
    other = service.create(review_spec("Another Workflow"))
    owner.open_workflow(other.document.graph.graph_id)
    frozen = owner.capture_explicit_workflow_context(
        first.document.graph.graph_id, model="deepseek-chat", thinking="off"
    )
    assert frozen.explicit_workflow_id == first.document.graph.graph_id
    assert frozen.workflows.ids == (first.document.graph.graph_id,)
    assert not owner._workflow_session.is_enabled()
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("Review password reset", True))
    card.run_button.click()
    assert runs == [(first.document.graph.graph_id, "Review password reset")]
    bridge.running = True
    bridge.started.emit()
    assert not card.run_button.isEnabled()
    bridge.running = False
    bridge.finished.emit()
    assert card.run_button.isEnabled()
    stale_button = card.run_button
    chat.reset()
    stale_button.click()
    assert len(runs) == 1
    assert controller.cards == ()
    owner.agents_page.close()
    owner.agents_page.deleteLater()
    chat.close()


class SavedCapture(QObject):
    def __init__(self):
        super().__init__()
        self.events = []

    @Slot(object)
    def saved(self, value):
        self.events.append((value, threading.get_ident()))


def test_real_bridge_authors_with_agents_off_and_drops_obsolete_events(qapp, tmp_path):
    service, _ = authoring_setup(tmp_path)
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(tmp_path / "project")
    bridge.set_workflow_authoring_provider(lambda: service)
    bridge.history.append_user_text("Create a reusable review Workflow.")
    capture = SavedCapture()
    bridge.workflowAuthored.connect(capture.saved)
    args = json.dumps(asdict(review_spec()))
    catalogs = []

    def stream(**kwargs):
        catalogs.append({tool["function"]["name"] for tool in kwargs.get("tools", [])})
        if len(catalogs) == 1:
            yield ToolCallStart(index=0, id="save-1", name="create_workflow")
            yield ToolCallArgsDelta(index=0, args_chunk=args)
            yield ToolCallEnd(index=0)
            yield Done(
                finish_reason="tool_calls",
                full_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "save-1",
                            "type": "function",
                            "function": {"name": "create_workflow", "arguments": args},
                        }
                    ],
                },
            )
        else:
            yield ContentDelta(text="Your Workflow is saved.")
            yield Done(finish_reason="stop", full_message={"role": "assistant", "content": "Saved."})

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    finished = []
    bridge.finished.connect(lambda: finished.append(True))
    try:
        bridge.send(model="test-model", thinking="off")
        deadline = time.monotonic() + 8
        while not finished and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert finished
        assert len(capture.events) == 1
        saved, thread = capture.events[0]
        assert thread == threading.get_ident()
        assert service.document(saved.document.graph.graph_id) == saved.document
        assert "create_workflow" in catalogs[0]
        assert "run_agent_team" not in catalogs[0]
        assert catalogs[0] == catalogs[1]
        assert bridge.registry._workflow_authoring is None
        assert bridge.registry.turn_agent_context.mode is AgentTurnMode.OFF
        generation = bridge._agent_team_generation
        bridge._invalidate_agent_team_presentation()
        bridge._on_workflow_authored(generation, saved)
        assert len(capture.events) == 1
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)
        bridge.shutdown()
