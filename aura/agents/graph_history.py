"""Undo and redo for one workflow, as a stack of whole graphs.

A :class:`~aura.agents.graph_models.WorkflowGraph` is immutable and small, so
history is kept as complete snapshots rather than as reversible commands.
That buys the one property an editing canvas most needs: undoing a move, a
deletion, a rewire, and an inserted node all restore *exactly* what was
there, with no per-operation inverse to get subtly wrong.

Nothing here is Qt-aware. The canvas asks for a state and redraws it; it
never edits one in place.
"""
from __future__ import annotations

from aura.agents.graph_models import WorkflowGraph

#: How many states back one workflow can be walked. Snapshots are cheap, but
#: not free, and nobody undoes two hundred moves.
DEFAULT_HISTORY_LIMIT = 100


class GraphHistory:
    """The states one workflow has been in, and where in them we are."""

    def __init__(
        self, graph: WorkflowGraph, *, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> None:
        self._limit = max(2, int(limit))
        self._states: list[WorkflowGraph] = [graph]
        self._index = 0

    @property
    def current(self) -> WorkflowGraph:
        return self._states[self._index]

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index < len(self._states) - 1

    @property
    def depth(self) -> int:
        return len(self._states)

    def reset(self, graph: WorkflowGraph) -> None:
        """Start again from *graph* — a different workflow was opened."""
        self._states = [graph]
        self._index = 0

    def push(self, graph: WorkflowGraph) -> bool:
        """Record *graph* as the new current state.

        A state identical to the current one is dropped, so a drag that ends
        where it started, or a save that changed nothing, does not leave an
        undo step that appears to do nothing.
        """
        if graph == self.current:
            return False
        del self._states[self._index + 1 :]
        self._states.append(graph)
        if len(self._states) > self._limit:
            del self._states[0 : len(self._states) - self._limit]
        self._index = len(self._states) - 1
        return True

    def undo(self) -> WorkflowGraph | None:
        """Step back one state, or None when there is nothing behind us."""
        if not self.can_undo:
            return None
        self._index -= 1
        return self.current

    def redo(self) -> WorkflowGraph | None:
        """Step forward one state, or None when there is nothing ahead."""
        if not self.can_redo:
            return None
        self._index += 1
        return self.current


__all__ = ["DEFAULT_HISTORY_LIMIT", "GraphHistory"]
