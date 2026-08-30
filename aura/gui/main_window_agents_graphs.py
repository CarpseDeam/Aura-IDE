"""Workflow-authoring cluster for the Agents page.

This controller connects one :class:`~aura.agents.graph_session.WorkflowSession`
to the three surfaces that show it: the workflow bar above, the canvas in the
middle, and the workflow half of the inspector on the right. The session owns
storage, undo, and this user's private choices; the canvas reports what the
user did; :mod:`aura.agents.graph_edits` says what each of those gestures
means as an edit. What is left here — deliberately all that is left — is
deciding when to redraw, what to put in the inspector, and how a person is
told when something is refused.

Agent definitions are read here, never written. A workflow points at agents by
id: dropping one places an occurrence, and deleting that occurrence removes
the box and nothing else. The definition it referred to is untouched, because
the same agent is very likely standing in three other workflows.

Nothing here runs anything. There is no runner, no play button, and no model
call in this phase — availability is recorded so the decision belongs to the
user from the start, and is read by nobody yet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from aura.agents import graph_edits
from aura.agents.graph_local_state import WorkflowLocalState, WorkflowLocalStateError
from aura.agents.graph_models import ConnectionKind, Point, WorkflowGraph
from aura.agents.graph_session import WorkflowSession
from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError
from aura.agents.graph_validation import (
    GraphValidation,
    reference_scope_error,
    validate_graph,
)
from aura.agents.identity import AgentScope
from aura.agents.store import AgentSummary
from aura.agents.validation import workflow_name_error
from aura.gui.agents_page import AgentsPage
from aura.gui.agents_workflow_bar import WorkflowRow
from aura.gui.agents_workflow_node import NODE_HEIGHT, NODE_WIDTH
from aura.gui.agents_workflow_presenter import (
    connection_info,
    node_visuals,
    occurrence_info,
    workflow_info,
)

logger = logging.getLogger(__name__)

_EMPTY_CANVAS = "No workflow open yet. Use New to start one."


class AgentsGraphController(QObject):
    """Drives the workflow surfaces from the one workflow that is open."""

    def __init__(
        self,
        page: AgentsPage,
        *,
        workspace_root: Path | None,
        agent_summaries: Callable[[], tuple[AgentSummary, ...]],
        mutations_allowed: Callable[[], bool],
        store_factory: Callable[[Path], AgentGraphStore] | None = None,
        state_factory: Callable[[Path], WorkflowLocalState] | None = None,
        parent_widget: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._page = page
        self._agent_summaries = agent_summaries
        self._mutations_allowed = mutations_allowed
        self._parent_widget = parent_widget
        self._session = WorkflowSession(
            workspace_root, store_factory=store_factory, state_factory=state_factory
        )
        self._render_queued = False
        self._connect_page()

    def _connect_page(self) -> None:
        bar = self._page.workflow_bar
        bar.workflow_selected.connect(self._on_workflow_selected)
        bar.create_requested.connect(self._on_create_requested)
        bar.rename_requested.connect(self._on_rename_requested)
        bar.delete_requested.connect(self._on_delete_workflow_requested)
        bar.availability_changed.connect(self._on_availability_changed)

        scene = self._page.scene
        scene.node_moved.connect(self._on_node_moved)
        scene.connect_requested.connect(self._on_connect_requested)
        scene.connection_rerouted.connect(self._on_connection_rerouted)
        scene.connection_reconnected.connect(self._on_connection_reconnected)
        scene.selection_changed.connect(self._on_selection_changed)
        scene.delete_requested.connect(self._on_delete_items_requested)
        scene.agent_dropped.connect(self._on_agent_dropped)

        self._page.view.undo_requested.connect(self.undo)
        self._page.view.redo_requested.connect(self.redo)

        inspector = self._page.inspector
        inspector.description_changed.connect(self._on_description_changed)
        inspector.assignment_changed.connect(self._on_assignment_changed)
        inspector.connection_order_changed.connect(self._on_connection_order_changed)
        inspector.connection_straighten_requested.connect(self._on_straighten_requested)
        inspector.connection_delete_requested.connect(self._on_connection_delete)

    # ---- lifecycle ---------------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        """Rebind to a new workspace, discarding the previous one's workflows."""
        self._session.rebind(root)
        self._page.set_workflow_rows((), "")
        self._page.set_workflow_info(None)
        self._page.set_occurrence(None)
        self._page.set_connection(None)
        self._page.scene.render_graph(None, {})

    @property
    def session(self) -> WorkflowSession:
        return self._session

    @property
    def current_graph(self) -> WorkflowGraph | None:
        return self._session.graph

    # ---- rendering ---------------------------------------------------------

    def refresh(self) -> None:
        """Reload the workflow list and the open workflow from disk."""
        if not self._session.bound:
            self.set_workspace_root(None)
            return
        self._session.reload()
        self._page.set_workflow_rows(
            tuple(
                WorkflowRow(
                    graph_id=row.graph_id,
                    scope=row.scope.value,
                    name=row.name,
                    valid=row.valid,
                    errors=row.errors,
                )
                for row in self._session.summaries
            ),
            self._session.graph_id,
        )
        self._page.set_workflow_available(self._session.is_available())
        self.render()

    def render(self) -> None:
        """Redraw the canvas and the inspector from the workflow in hand."""
        graph = self._session.graph
        if graph is None:
            summary = self._session.summary
            self._page.scene.render_graph(None, {})
            self._page.set_workflow_info(None)
            self._page.set_occurrence(None)
            self._page.set_connection(None)
            self._page.scene.set_placeholder(
                "\n".join(summary.errors)
                if summary is not None and summary.errors
                else _EMPTY_CANVAS
            )
            return
        agents = self._agent_index()
        verdict = self._validate(graph)
        self._page.scene.render_graph(graph, node_visuals(graph, agents, verdict), verdict)
        self._page.set_workflow_info(workflow_info(graph, verdict))
        self._sync_inspector(verdict)

    def _schedule_render(self) -> None:
        """Redraw after the current event finishes.

        Canvas intents arrive from inside an item's own mouse handler, and a
        rebuild destroys those items. Deferring by one turn of the event loop
        lets Qt finish delivering before the scene is torn down.
        """
        if self._render_queued:
            return
        self._render_queued = True

        def _run() -> None:
            self._render_queued = False
            self.render()

        QTimer.singleShot(0, _run)

    def _agent_index(self) -> dict[str, AgentSummary]:
        try:
            return {summary.agent_id: summary for summary in self._agent_summaries()}
        except Exception:
            logger.debug("agents: could not read the agent library", exc_info=True)
            return {}

    def _validate(self, graph: WorkflowGraph) -> GraphValidation:
        scopes = {
            summary.agent_id: summary.scope
            for summary in self._agent_index().values()
            if summary.valid
        }
        return validate_graph(graph, agents=scopes)

    def _sync_inspector(self, verdict: GraphValidation) -> None:
        graph = self._session.graph
        if graph is None:
            return
        agents = self._agent_index()
        nodes, edges = self._page.scene.selected_ids()
        node = graph.node(nodes[0]) if len(nodes) == 1 else None
        edge = graph.connection(edges[0]) if len(edges) == 1 else None
        self._page.set_occurrence(
            occurrence_info(graph, node, agents, verdict) if node is not None else None
        )
        self._page.set_connection(
            connection_info(graph, edge, agents, verdict) if edge is not None else None
        )

    # ---- workflow lifecycle ------------------------------------------------

    def _on_workflow_selected(self, graph_id: str) -> None:
        if graph_id == self._session.graph_id:
            return
        self._session.open(str(graph_id))
        self.refresh()

    def _on_create_requested(self, scope_key: str) -> None:
        if not self._mutations_allowed():
            return
        try:
            scope = AgentScope(scope_key)
        except ValueError:
            return
        if self._guarded(lambda: self._session.create(scope)) is not None:
            self.refresh()

    def _on_rename_requested(self) -> None:
        graph = self._session.graph
        if graph is None or not self._mutations_allowed():
            return
        name, accepted = QInputDialog.getText(
            self._parent_widget, "Rename workflow", "Name", text=graph.name
        )
        if not accepted:
            return
        error = workflow_name_error(name)
        if error:
            self._warn(error)
            return
        if self._guarded(lambda: self._session.rename(name)):
            self.refresh()

    def _on_delete_workflow_requested(self) -> None:
        summary = self._session.summary
        if summary is None or not self._mutations_allowed():
            return
        if not self._confirm(
            "Delete workflow",
            f"Delete “{summary.name}”? Its file is removed from disk. The agents "
            "it used are not affected.",
        ):
            return
        self._guarded(self._session.delete)
        self.refresh()

    def _on_availability_changed(self, available: bool) -> None:
        if not self._mutations_allowed():
            return
        self._guarded(lambda: self._session.set_available(bool(available)))
        self._page.set_workflow_available(self._session.is_available())

    # ---- canvas intents ----------------------------------------------------

    def _on_node_moved(self, node_id: str, x: float, y: float) -> None:
        graph = self._session.graph
        if graph is None:
            return
        # The scene already shows the node where it was dropped, so this only
        # records it — redrawing here would fight the drag that just ended.
        self._apply(graph_edits.move_node(graph, node_id, x, y), render=False)

    def _on_connect_requested(
        self, source_id: str, target_id: str, kind_value: str
    ) -> None:
        graph = self._session.graph
        kind = ConnectionKind.parse(kind_value)
        if graph is None or kind is None:
            return
        self._apply(graph_edits.connect(graph, source_id, target_id, kind))

    def _on_connection_rerouted(self, connection_id: str, bend: object) -> None:
        graph = self._session.graph
        if graph is None:
            return
        point = bend if isinstance(bend, Point) else None
        # The line is already drawn bent; this only records where it was put.
        self._apply(graph_edits.set_bend(graph, connection_id, point), render=False)

    def _on_connection_reconnected(
        self, connection_id: str, end: str, node_id: str
    ) -> None:
        graph = self._session.graph
        if graph is None:
            return
        # A refused reconnection still needs a redraw: the line is currently
        # drawn hanging from the cursor and must snap back to where it was.
        if not self._apply(graph_edits.reconnect(graph, connection_id, end, node_id)):
            self._schedule_render()

    def _on_delete_items_requested(
        self, node_ids: object, connection_ids: object
    ) -> None:
        graph = self._session.graph
        if graph is None:
            return
        self._apply(
            graph_edits.remove(graph, tuple(node_ids or ()), tuple(connection_ids or ()))
        )

    def _on_agent_dropped(
        self, source_key: str, x: float, y: float, connection_id: str
    ) -> None:
        graph = self._session.graph
        if graph is None or not self._mutations_allowed():
            return
        scope_key, _, agent_id = str(source_key).partition(":")
        summary = self._agent_index().get(agent_id)
        if summary is None or not summary.valid or summary.scope.value != scope_key:
            return
        error = reference_scope_error(graph.scope, summary.scope)
        if error:
            self._warn(error[0].upper() + error[1:] + ".")
            return

        placed, node_id = graph_edits.place_agent(
            graph, agent_id, float(x) - NODE_WIDTH / 2.0, float(y) - NODE_HEIGHT / 2.0
        )
        if connection_id:
            placed = graph_edits.insert_into_step(placed, connection_id, node_id) or placed
        if self._apply(placed):
            QTimer.singleShot(0, lambda: self._page.scene.select_node(node_id))

    # ---- selection ---------------------------------------------------------

    def _on_selection_changed(self, kind: str, item_id: str) -> None:
        graph = self._session.graph
        if graph is None:
            self._page.set_occurrence(None)
            self._page.set_connection(None)
            return
        verdict = self._validate(graph)
        agents = self._agent_index()
        if kind == "node":
            node = graph.node(item_id)
            self._page.set_connection(None)
            self._page.set_occurrence(
                occurrence_info(graph, node, agents, verdict) if node else None
            )
            if node is not None and node.is_agent:
                summary = agents.get(node.agent_id)
                if summary is not None:
                    self._page.select_agent(node.agent_id, summary.scope.value)
            return
        if kind == "connection":
            edge = graph.connection(item_id)
            self._page.set_occurrence(None)
            self._page.set_connection(
                connection_info(graph, edge, agents, verdict) if edge else None
            )
            return
        self._page.set_occurrence(None)
        self._page.set_connection(None)

    def on_library_selection(self, agent_id: str) -> None:
        """Clear a canvas selection that no longer matches the library cursor."""
        occurrence = self._page.inspector.occurrence
        if occurrence is None or not agent_id:
            return
        graph = self._session.graph
        node = graph.node(occurrence.node_id) if graph is not None else None
        if node is not None and node.agent_id == agent_id:
            return
        self._page.scene.clearSelection()

    # ---- inspector intents -------------------------------------------------

    def _on_description_changed(self, text: str) -> None:
        graph = self._session.graph
        if graph is None:
            return
        self._apply(graph.with_name(graph.name, text), defer=False)

    def _on_assignment_changed(self, node_id: str, text: str) -> None:
        graph = self._session.graph
        if graph is None:
            return
        self._apply(graph_edits.set_assignment(graph, node_id, text), defer=False)

    def _on_connection_order_changed(self, connection_id: str, order: int) -> None:
        graph = self._session.graph
        if graph is None:
            return
        self._apply(graph_edits.set_order(graph, connection_id, order))

    def _on_straighten_requested(self, connection_id: str) -> None:
        graph = self._session.graph
        if graph is None:
            return
        self._apply(graph_edits.set_bend(graph, connection_id, None))

    def _on_connection_delete(self, connection_id: str) -> None:
        graph = self._session.graph
        if graph is None:
            return
        self._apply(graph_edits.remove(graph, (), (connection_id,)))

    # ---- undo and redo -----------------------------------------------------

    def undo(self) -> None:
        self._step_history(redo=False)

    def redo(self) -> None:
        self._step_history(redo=True)

    def _step_history(self, *, redo: bool) -> None:
        if not self._mutations_allowed():
            return
        stepped = self._guarded(self._session.redo if redo else self._session.undo)
        if stepped is not None:
            self._schedule_render()

    # ---- applying an edit --------------------------------------------------

    def _apply(
        self, graph: WorkflowGraph | None, *, render: bool = True, defer: bool = True
    ) -> bool:
        """Save an edit and redraw, refusing everything during a running turn.

        ``render=False`` is for edits the canvas has already drawn for itself —
        a node let go, a line bent — where redrawing would only fight what the
        user is looking at.
        """
        if graph is None or not self._mutations_allowed():
            return False
        if not self._guarded(lambda: self._session.commit(graph)):
            return False
        if render:
            if defer:
                self._schedule_render()
            else:
                self.render()
        return True

    def _guarded(self, action: Callable[[], object]) -> object:
        """Run one storage operation, turning a refusal into a visible message."""
        try:
            return action()
        except (AgentGraphStoreError, WorkflowLocalStateError) as exc:
            self._warn(str(exc))
            return None

    # ---- dialogs -----------------------------------------------------------

    def _confirm(self, title: str, message: str) -> bool:
        if self._parent_widget is None:
            return True
        reply = QMessageBox.question(
            self._parent_widget,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _warn(self, message: str) -> None:
        if self._parent_widget is None:
            logger.warning("Agents: %s", message)
            return
        QMessageBox.warning(self._parent_widget, "Agents", message)


__all__ = ["AgentsGraphController"]
