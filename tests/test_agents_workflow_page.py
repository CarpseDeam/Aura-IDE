"""Authoring a workflow through the Agents page.

The page and its canvas render what they are handed and emit what the user
did; the controllers decide and write. So every assertion below is either
about what a user would see on the canvas or about what actually landed in a
workflow file or in this user's private state — never about a value a widget
invented for itself.

The rule this file exists to hold down: a canvas occurrence is not an agent.
Placing one twice makes two of them, giving one an assignment leaves the
other alone, and deleting one never touches the definition either of them
points at.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QMimeData, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from aura.agents.graph_local_state import WorkflowLocalState  # noqa: E402
from aura.agents.graph_models import ConnectionKind, Point  # noqa: E402
from aura.agents.graph_store import AgentGraphStore  # noqa: E402
from aura.agents.graph_validation import MISSING_AGENT_LABEL  # noqa: E402
from aura.agents.local_state import AgentLocalState  # noqa: E402
from aura.agents.models import AgentScope  # noqa: E402
from aura.agents.store import AgentStore  # noqa: E402
from aura.gui.agents_library import AGENT_MIME  # noqa: E402
from aura.gui.agents_workflow_node import NODE_HEIGHT, NODE_WIDTH  # noqa: E402
from aura.gui.main_window_agents import MainWindowAgentsController  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def wired(tmp_path: Path, qapp, monkeypatch) -> SimpleNamespace:
    """The Agents page as MainWindow builds it, against a throwaway workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    personal = tmp_path / "personal"
    workflows = tmp_path / "workflows"
    userdata = tmp_path / "userdata"

    agents = AgentStore(workspace, personal_dir=personal)

    def _scopes() -> dict[str, AgentScope]:
        return {row.agent_id: row.scope for row in agents.list_summaries() if row.valid}

    window = SimpleNamespace(
        _workspace_root=workspace, _edge_rail=SimpleNamespace(agents_tab=None)
    )
    controller = MainWindowAgentsController(
        window,
        workspace_root=workspace,
        store_factory=lambda root: AgentStore(root, personal_dir=personal),
        state_factory=lambda root: AgentLocalState(root, state_root=userdata),
        graph_store_factory=lambda root: AgentGraphStore(
            root, personal_dir=workflows, agent_scopes=_scopes
        ),
        workflow_state_factory=lambda root: WorkflowLocalState(
            root, state_root=userdata
        ),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *args, **kwargs: None))
    harness = SimpleNamespace(
        app=qapp,
        workspace=workspace,
        controller=controller,
        agents=agents,
        graphs=AgentGraphStore(workspace, personal_dir=workflows),
        state=WorkflowLocalState(workspace, state_root=userdata),
    )
    yield harness
    page = controller.agents_page
    if page is not None:
        page.close()
        page.deleteLater()


def _agent(store: AgentStore, scope: AgentScope, name: str) -> str:
    return store.create(
        scope, name=name, description=f"{name} does one thing.", instructions=f"Be {name}."
    ).agent_id


def _open(wired: SimpleNamespace):
    wired.controller.on_agents_requested()
    return wired.controller.agents_page, wired.controller.graphs


def _new_workflow(wired: SimpleNamespace, scope: str = "project"):
    page, graphs = _open(wired)
    page.workflow_bar.create_requested.emit(scope)
    wired.app.processEvents()
    return page, graphs


def _drop(wired: SimpleNamespace, page, agent_id: str, x: float, y: float, edge: str = ""):
    page.scene.agent_dropped.emit(f"project:{agent_id}", x, y, edge)
    wired.app.processEvents()


def _drag_from_library(wired: SimpleNamespace, page, source_key: str, scene_point):
    """A real drop of a library row onto the canvas, at a scene coordinate."""
    payload = QMimeData()
    payload.setData(AGENT_MIME, source_key.encode("utf-8"))
    event = QDropEvent(
        QPointF(page.view.mapFromScene(scene_point)),
        Qt.DropAction.CopyAction,
        payload,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    page.view.dropEvent(event)
    wired.app.processEvents()
    return event


# ── creating and choosing a workflow ─────────────────────────────────────────


def test_creating_a_workflow_writes_a_file_and_opens_it(wired) -> None:
    page, graphs = _new_workflow(wired)

    graph = graphs.current_graph
    assert graph is not None
    assert wired.graphs.path_for(AgentScope.PROJECT, graph.graph_id).is_file()
    assert page.current_workflow_id() == graph.graph_id
    assert page.scene.node_items.keys() == {
        graph.task_node.node_id,
        graph.result_node.node_id,
    }


def test_the_open_workflow_is_remembered_for_next_time(wired) -> None:
    _page, graphs = _new_workflow(wired)

    assert wired.state.selected_id() == graphs.current_graph.graph_id


def test_a_personal_workflow_is_created_outside_the_project(wired) -> None:
    _page, graphs = _new_workflow(wired, "personal")

    graph = graphs.current_graph
    assert graph.scope is AgentScope.PERSONAL
    assert not (wired.workspace / ".aura" / "agents" / "workflows").exists()


def test_deleting_a_workflow_forgets_every_private_decision_about_it(wired) -> None:
    page, graphs = _new_workflow(wired)
    graph_id = graphs.current_graph.graph_id
    page.workflow_bar.availability_changed.emit(True)

    page.workflow_bar.delete_requested.emit()
    wired.app.processEvents()

    assert wired.graphs.get(graph_id) is None
    assert wired.state.available_ids() == ()
    assert wired.state.selected_id() == ""


# ── placing agents ───────────────────────────────────────────────────────────


def test_a_library_row_carries_only_the_agent_scope_and_id(wired) -> None:
    agent_id = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, _graphs = _open(wired)

    payload = page._library.tree.mimeData([page._items[f"project:{agent_id}"]])

    assert bytes(payload.data(AGENT_MIME)).decode() == f"project:{agent_id}"


def test_dragging_a_row_onto_the_canvas_places_it_where_it_was_dropped(wired) -> None:
    agent_id = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    page.resize(1200, 700)
    page.show()
    wired.app.processEvents()

    event = _drag_from_library(wired, page, f"project:{agent_id}", QPointF(0.0, 140.0))

    (placed,) = [node for node in graphs.current_graph.nodes if node.is_agent]
    assert event.isAccepted() is True
    assert placed.agent_id == agent_id
    # The box is centred on the cursor rather than starting there.
    assert placed.position.x == pytest.approx(-NODE_WIDTH / 2.0, abs=1.0)
    assert placed.position.y == pytest.approx(140.0 - NODE_HEIGHT / 2.0, abs=1.0)


def test_dropping_an_agent_places_an_occurrence_that_only_references_it(wired) -> None:
    agent_id = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)

    _drop(wired, page, agent_id, 40.0, 10.0)

    graph = graphs.current_graph
    (placed,) = [node for node in graph.nodes if node.is_agent]
    assert placed.agent_id == agent_id
    assert placed.assignment == ""
    text = wired.graphs.path_for(AgentScope.PROJECT, graph.graph_id).read_text("utf-8")
    assert "Be Reviewer" not in text


def test_the_same_agent_placed_twice_is_two_independent_occurrences(wired) -> None:
    agent_id = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)

    _drop(wired, page, agent_id, -40.0, 0.0)
    _drop(wired, page, agent_id, 60.0, 90.0)
    first, second = [node for node in graphs.current_graph.nodes if node.is_agent]
    page.scene.select_node(second.node_id)
    page.inspector.assignment.setPlainText("Read it again.")
    page.inspector.apply_assignment_button.click()
    wired.app.processEvents()

    graph = graphs.current_graph
    assert first.node_id != second.node_id
    assert graph.node(second.node_id).assignment == "Read it again."
    assert graph.node(first.node_id).assignment == ""
    assert graph.agent_ids == (agent_id,)


def test_removing_a_canvas_node_never_deletes_the_reusable_agent(wired) -> None:
    agent_id = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, agent_id, 0.0, 0.0)
    (placed,) = [node for node in graphs.current_graph.nodes if node.is_agent]

    page.scene.delete_requested.emit((placed.node_id,), ())
    wired.app.processEvents()

    assert graphs.current_graph.node(placed.node_id) is None
    assert wired.agents.get(agent_id) is not None
    assert page.visible_agent_ids()["project"] == (agent_id,)


def test_the_two_fixed_nodes_cannot_be_deleted(wired) -> None:
    page, graphs = _new_workflow(wired)
    task = graphs.current_graph.task_node

    page.scene.select_node(task.node_id)
    page.scene.delete_selection()
    wired.app.processEvents()

    assert graphs.current_graph.node(task.node_id) is not None


def test_a_project_workflow_refuses_a_personal_agent(wired) -> None:
    personal_id = _agent(wired.agents, AgentScope.PERSONAL, "Scout")
    page, graphs = _new_workflow(wired)

    page.scene.agent_dropped.emit(f"personal:{personal_id}", 0.0, 0.0, "")
    wired.app.processEvents()

    assert [node for node in graphs.current_graph.nodes if node.is_agent] == []


# ── connecting them ──────────────────────────────────────────────────────────


def test_the_right_port_makes_a_step_and_the_bottom_port_makes_a_sub_agent(
    wired,
) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    scout = _agent(wired.agents, AgentScope.PROJECT, "Scout")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 0.0, 0.0)
    _drop(wired, page, scout, 0.0, 160.0)
    step_node, helper = [node for node in graphs.current_graph.nodes if node.is_agent]
    graph = graphs.current_graph

    page.scene.connect_requested.emit(graph.task_node.node_id, step_node.node_id, "step")
    wired.app.processEvents()
    page.scene.connect_requested.emit(step_node.node_id, graph.result_node.node_id, "step")
    wired.app.processEvents()
    page.scene.connect_requested.emit(step_node.node_id, helper.node_id, "sub_agent")
    wired.app.processEvents()

    graph = graphs.current_graph
    assert len(graph.connections_of_kind(ConnectionKind.STEP)) == 2
    (dashed,) = graph.connections_of_kind(ConnectionKind.SUB_AGENT)
    assert (dashed.source_id, dashed.target_id) == (step_node.node_id, helper.node_id)
    assert wired.graphs.get(graph.graph_id) == graph


def test_a_second_next_step_replaces_the_first_rather_than_forking(wired) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    scout = _agent(wired.agents, AgentScope.PROJECT, "Scout")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 0.0, 0.0)
    _drop(wired, page, scout, 0.0, 140.0)
    first, second = [node for node in graphs.current_graph.nodes if node.is_agent]
    task = graphs.current_graph.task_node

    page.scene.connect_requested.emit(task.node_id, first.node_id, "step")
    wired.app.processEvents()
    page.scene.connect_requested.emit(task.node_id, second.node_id, "step")
    wired.app.processEvents()

    steps = graphs.current_graph.outgoing(task.node_id, ConnectionKind.STEP)
    assert [edge.target_id for edge in steps] == [second.node_id]


def test_dropping_an_agent_onto_a_solid_connection_inserts_it_into_the_path(
    wired,
) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    graph = graphs.current_graph
    page.scene.connect_requested.emit(
        graph.task_node.node_id, graph.result_node.node_id, "step"
    )
    wired.app.processEvents()
    (edge,) = graphs.current_graph.connections

    _drop(wired, page, reviewer, 0.0, 0.0, edge.connection_id)

    graph = graphs.current_graph
    (placed,) = [node for node in graph.nodes if node.is_agent]
    assert [
        (item.source_id, item.target_id)
        for item in sorted(graph.connections, key=lambda item: item.order)
    ] == [
        (graph.task_node.node_id, placed.node_id),
        (placed.node_id, graph.result_node.node_id),
    ]


def test_selecting_a_connection_lets_it_be_straightened_and_removed(wired) -> None:
    page, graphs = _new_workflow(wired)
    graph = graphs.current_graph
    page.scene.connect_requested.emit(
        graph.task_node.node_id, graph.result_node.node_id, "step"
    )
    wired.app.processEvents()
    (edge,) = graphs.current_graph.connections

    page.scene.connection_rerouted.emit(edge.connection_id, Point(18.0, -44.0))
    assert graphs.current_graph.connection(edge.connection_id).bend == Point(18.0, -44.0)
    assert wired.graphs.get(graph.graph_id).connection(edge.connection_id).bend is not None

    page.inspector.connection_straighten_requested.emit(edge.connection_id)
    wired.app.processEvents()
    assert graphs.current_graph.connection(edge.connection_id).bend is None

    page.inspector.connection_delete_requested.emit(edge.connection_id)
    wired.app.processEvents()
    assert graphs.current_graph.connections == ()


def test_reconnecting_an_end_keeps_the_same_connection(wired) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 0.0, 0.0)
    graph = graphs.current_graph
    (placed,) = [node for node in graph.nodes if node.is_agent]
    page.scene.connect_requested.emit(
        graph.task_node.node_id, graph.result_node.node_id, "step"
    )
    wired.app.processEvents()
    (edge,) = graphs.current_graph.connections

    page.scene.connection_reconnected.emit(edge.connection_id, "target", placed.node_id)
    wired.app.processEvents()

    moved = graphs.current_graph.connection(edge.connection_id)
    assert moved is not None
    assert moved.target_id == placed.node_id


# ── what is saved, and what comes back ───────────────────────────────────────


def test_a_moved_node_keeps_its_place_across_a_reload(wired) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 0.0, 0.0)
    (placed,) = [node for node in graphs.current_graph.nodes if node.is_agent]
    graph_id = graphs.current_graph.graph_id

    page.scene.node_moved.emit(placed.node_id, 128.0, -64.0)
    wired.app.processEvents()

    reloaded = wired.graphs.get(graph_id)
    assert reloaded.node(placed.node_id).position == Point(128.0, -64.0)


def test_reopening_the_page_redraws_the_saved_workflow(wired) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 20.0, 20.0)
    graph = graphs.current_graph

    wired.controller.on_agents_requested()  # hide
    wired.controller.on_agents_requested()  # show again
    wired.app.processEvents()

    assert graphs.current_graph == graph
    assert len(page.scene.node_items) == 3


def test_an_agent_deleted_from_the_library_stays_visible_as_missing(wired) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 0.0, 0.0)
    (placed,) = [node for node in graphs.current_graph.nodes if node.is_agent]

    wired.agents.delete(AgentScope.PROJECT, reviewer)
    wired.controller.refresh()
    wired.app.processEvents()

    assert graphs.current_graph.node(placed.node_id).agent_id == reviewer
    visual = page.scene.node_items[placed.node_id].visual
    assert visual.missing is True
    assert visual.title == MISSING_AGENT_LABEL


# ── undo, redo, and a running turn ───────────────────────────────────────────


def test_undo_and_redo_walk_the_whole_workflow_back_and_forward(wired) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 0.0, 0.0)
    graph_id = graphs.current_graph.graph_id
    placed = [node for node in graphs.current_graph.nodes if node.is_agent]

    graphs.undo()
    wired.app.processEvents()
    assert [node for node in graphs.current_graph.nodes if node.is_agent] == []
    assert wired.graphs.get(graph_id).nodes_of_kind(placed[0].kind) == ()

    graphs.redo()
    wired.app.processEvents()
    assert [node.node_id for node in graphs.current_graph.nodes if node.is_agent] == [
        placed[0].node_id
    ]


def test_a_turn_freezes_the_canvas_but_keeps_it_readable(wired) -> None:
    reviewer = _agent(wired.agents, AgentScope.PROJECT, "Reviewer")
    page, graphs = _new_workflow(wired)
    _drop(wired, page, reviewer, 0.0, 0.0)
    before = graphs.current_graph

    wired.controller.set_execution_active(True)
    page.scene.agent_dropped.emit(f"project:{reviewer}", 90.0, 90.0, "")
    page.workflow_bar.create_requested.emit("project")
    wired.app.processEvents()

    assert page.scene.editable is False
    assert page.workflow_bar.new_button.isEnabled() is False
    assert page.inspector.apply_assignment_button.isEnabled() is False
    assert graphs.current_graph == before
    assert len(page.scene.node_items) == 3

    wired.controller.set_execution_active(False)
    assert page.scene.editable is True


# ── the workflow-level switch ────────────────────────────────────────────────


def test_making_a_workflow_available_writes_only_private_state(wired) -> None:
    page, graphs = _new_workflow(wired)
    graph_id = graphs.current_graph.graph_id
    path = wired.graphs.path_for(AgentScope.PROJECT, graph_id)
    before = path.read_text(encoding="utf-8")

    page.workflow_bar.availability_changed.emit(True)

    assert wired.state.is_available(graph_id) is True
    assert path.read_text(encoding="utf-8") == before
    assert page.workflow_bar.available.isChecked() is True

    page.workflow_bar.availability_changed.emit(False)
    assert wired.state.available_ids() == ()
