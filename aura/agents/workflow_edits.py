"""One serialized save and undo owner for canvas and conversational edits."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from aura.agents.graph_history import GraphHistory
from aura.agents.graph_models import WorkflowGraph
from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError


class WorkflowEdits:
    """History is per identity, independent of the editor's current selection."""

    def __init__(self, store: Callable[[], AgentGraphStore | None]) -> None:
        self._store = store
        self._histories: dict[str, GraphHistory] = {}
        self.lock = RLock()

    def _require_store(self) -> AgentGraphStore:
        store = self._store()
        if store is None:
            raise AgentGraphStoreError("There is no workspace for this Workflow.")
        return store

    def check(self, expected: WorkflowGraph) -> None:
        """Compare the entire graph, including layout changed in the canvas."""
        if self._require_store().get(expected.graph_id) != expected:
            raise AgentGraphStoreError("This Workflow changed after it was inspected. Inspect it again before editing.")

    def history(self, graph: WorkflowGraph) -> GraphHistory:
        with self.lock:
            # A canvas/card may still display the previous revision. Reading its
            # undo state must not replace the history another surface just saved.
            graph = self._require_store().get(graph.graph_id) or graph
            history = self._histories.get(graph.graph_id)
            if history is None or history.current != graph:
                history = self._histories[graph.graph_id] = GraphHistory(graph)
            return history

    def commit(self, before: WorkflowGraph, after: WorkflowGraph) -> bool:
        with self.lock:
            if (before.graph_id, before.scope) != (after.graph_id, after.scope):
                raise AgentGraphStoreError("An edit must preserve the Workflow's identity and scope.")
            self.check(before)
            if before == after:
                return False
            history = self.history(before)
            self._require_store().save(after)
            history.push(after)
            return True

    def step(self, expected: WorkflowGraph, *, redo: bool = False) -> WorkflowGraph | None:
        with self.lock:
            self.check(expected)
            history = self.history(expected)
            graph = history.redo() if redo else history.undo()
            if graph is None:
                return None
            try:
                self._require_store().save(graph)
            except Exception:
                # Failed disk writes never advance the shared history cursor.
                history.undo() if redo else history.redo()
                raise
            return graph
