"""The workflow that is open, and everything that can happen to it.

One workspace's workflows, the one currently being authored, the undo stack
behind it, and this user's private editor selection. The workspace-level state
also records the single Agents gate; editor selection never changes its meaning.
Holding those together is what lets the page's controller be about drawing and
signals rather than about storage, and it is why this object outlives the
Agents window: the toolbar's Agents switch has to be answerable before anyone
has opened it.

Nothing here is Qt-aware, so the whole lifecycle of a workflow — created,
renamed, edited, undone, switched off, deleted — is exercisable without a
widget. Failures are raised, not swallowed: the caller owns how a person is
told, because only it knows whether there is a window to tell them in.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from aura.agents.graph_local_state import WorkflowLocalState, WorkflowLocalStateError
from aura.agents.graph_models import WorkflowGraph
from aura.agents.graph_store import (
    AgentGraphStore,
    AgentGraphStoreError,
    WorkflowSummary,
)
from aura.agents.identity import AgentScope
from aura.agents.workflow_edits import WorkflowEdits

logger = logging.getLogger(__name__)


class WorkflowSession:
    """One workspace's workflows, and the state of the one being authored."""

    def __init__(
        self,
        workspace_root: Path | None,
        *,
        store_factory: Callable[[Path], AgentGraphStore] | None = None,
        state_factory: Callable[[Path], WorkflowLocalState] | None = None,
    ) -> None:
        self._store_factory = store_factory or AgentGraphStore
        self._state_factory = state_factory or WorkflowLocalState
        self._workspace_root: Path | None = None
        self._summaries: dict[str, WorkflowSummary] = {}
        self._order: tuple[str, ...] = ()
        self._graph: WorkflowGraph | None = None
        self.edits: WorkflowEdits
        self._graph_id = ""
        self.rebind(workspace_root)

    # ---- binding -----------------------------------------------------------

    def rebind(self, workspace_root: Path | None) -> None:
        """Point at a different workspace, keeping nothing from the last one."""
        self._workspace_root = Path(workspace_root) if workspace_root else None
        self._summaries = {}
        self._order = ()
        self._graph = None
        self._graph_id = ""
        edit_root = self._workspace_root
        self.edits = WorkflowEdits(lambda: self._store_factory(edit_root) if edit_root else None)

    @property
    def bound(self) -> bool:
        return self._workspace_root is not None

    def store(self) -> AgentGraphStore | None:
        if self._workspace_root is None:
            return None
        try:
            return self._store_factory(self._workspace_root)
        except Exception:
            logger.debug("agents: could not bind AgentGraphStore", exc_info=True)
            return None

    def state(self) -> WorkflowLocalState | None:
        if self._workspace_root is None:
            return None
        try:
            return self._state_factory(self._workspace_root)
        except Exception:
            logger.debug("agents: could not bind WorkflowLocalState", exc_info=True)
            return None

    # ---- what is open ------------------------------------------------------

    @property
    def graph(self) -> WorkflowGraph | None:
        return self._graph

    @property
    def graph_id(self) -> str:
        return self._graph_id

    @property
    def summary(self) -> WorkflowSummary | None:
        return self._summaries.get(self._graph_id)

    @property
    def summaries(self) -> tuple[WorkflowSummary, ...]:
        return tuple(self._summaries[graph_id] for graph_id in self._order)

    @property
    def can_undo(self) -> bool:
        return self._graph is not None and self.edits.history(self._graph).can_undo

    @property
    def can_redo(self) -> bool:
        return self._graph is not None and self.edits.history(self._graph).can_redo

    # ---- reading from disk -------------------------------------------------

    def reload(self) -> None:
        """Re-read every workflow, and the one that should be open."""
        store = self.store()
        state = self.state()
        if store is None or state is None:
            self.rebind(self._workspace_root)
            return
        try:
            rows = store.list_summaries()
        except Exception:
            logger.debug("agents: could not list workflows", exc_info=True)
            rows = ()
        self._summaries = {row.graph_id: row for row in rows}
        self._order = tuple(row.graph_id for row in rows)

        chosen = self._choose(state)
        if chosen != self._graph_id:
            self._graph_id = chosen
        # The session's open graph is only the editor selection. Persist a
        # fallback selection as well as an explicit click.
        try:
            if state.selected_id() != chosen:
                state.set_selected(chosen)
        except WorkflowLocalStateError:
            logger.debug("agents: could not remember the selected workflow")
        row = self._summaries.get(chosen)
        self._graph = row.graph if row is not None else None

    def _choose(self, state: WorkflowLocalState) -> str:
        """Keep what is open; otherwise what was open; otherwise the first."""
        if self._graph_id in self._summaries:
            return self._graph_id
        try:
            remembered = state.selected_id()
        except Exception:
            remembered = ""
        if remembered in self._summaries:
            return remembered
        return next((row for row in self._order if self._summaries[row].valid), "")

    def open(self, graph_id: str) -> None:
        """Switch to another workflow and remember it for next time."""
        if str(graph_id) == self._graph_id:
            return
        self._graph_id = str(graph_id)
        self._remember_selection(self._graph_id)
        self.reload()

    # ---- lifecycle ---------------------------------------------------------

    def create(self, scope: AgentScope) -> WorkflowGraph:
        """Make a new workflow and open it. Raises AgentGraphStoreError."""
        store = self.store()
        if store is None:
            raise AgentGraphStoreError("There is no workspace to create a workflow in.")
        graph = store.create(scope)
        self._graph_id = graph.graph_id
        self._remember_selection(graph.graph_id)
        self.reload()
        return graph

    def rename(self, name: str) -> bool:
        """Give the open workflow a new name, keeping its id and its file."""
        graph = self._graph
        if graph is None:
            return False
        return self.commit(graph.with_name(name))

    def delete(self) -> bool:
        """Remove the open workflow, and every private decision about it."""
        store = self.store()
        row = self.summary
        if store is None or row is None:
            return False
        removed = store.delete(row.scope, row.graph_id)
        if removed:
            state = self.state()
            if state is not None:
                state.forget(row.graph_id)
        self._graph_id = ""
        self._graph = None
        self.reload()
        return removed

    # ---- the master gate ---------------------------------------------------

    def is_enabled(self) -> bool:
        """Whether the workspace's complete Agent capability is enabled."""
        state = self.state()
        if state is None:
            return False
        try:
            return state.is_enabled()
        except WorkflowLocalStateError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the workspace's complete Agent capability."""
        state = self.state()
        if state is None:
            return
        state.set_enabled(bool(enabled))

    def _remember_selection(self, graph_id: str) -> None:
        state = self.state()
        if state is None:
            return
        try:
            state.set_selected(graph_id)
        except WorkflowLocalStateError:
            logger.debug("agents: could not remember the open workflow")

    # ---- editing -----------------------------------------------------------

    def commit(self, graph: WorkflowGraph) -> bool:
        """Use the same explicit-identity edit owner as conversational tools."""
        if self._graph is None:
            return False
        changed = self.edits.commit(self._graph, graph)
        self._graph = graph
        self._refresh_summary(graph)
        return changed

    def undo(self) -> WorkflowGraph | None:
        return self._step(redo=False)

    def redo(self) -> WorkflowGraph | None:
        return self._step(redo=True)

    def _step(self, *, redo: bool) -> WorkflowGraph | None:
        if self._graph is None:
            return None
        graph = self.edits.step(self._graph, redo=redo)
        if graph is not None:
            self._graph = graph
            self._refresh_summary(graph)
        return graph

    def _refresh_summary(self, graph: WorkflowGraph) -> None:
        """Keep the listed row in step with what was just written."""
        row = self._summaries.get(graph.graph_id)
        if row is None:
            return
        self._summaries[graph.graph_id] = WorkflowSummary(
            graph_id=graph.graph_id,
            scope=graph.scope,
            name=graph.name,
            description=graph.description,
            valid=True,
            graph=graph,
            source=row.source,
        )


__all__ = ["WorkflowSession"]
