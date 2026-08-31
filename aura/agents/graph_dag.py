"""The solid DAG a workflow draws, read once and answered from consistently.

A workflow's solid lines are a *directed acyclic graph* from the one Task to
the one Aura Result: work fans out into branches that run independently, and
fans back in at joins that wait for every branch feeding them. This module is
the single place that reads those lines and says what shape they make, so the
canvas, the validator, the frozen plan, and the runner can never disagree
about which Step follows which.

Two things are settled here and nowhere else:

* **which Steps a Step waits for**, in the workflow's own persisted order, so
  a join hands its agent the same bundle in the same order on every run; and
* **the order the Steps are considered in**, a stable topological order seeded
  by the order the nodes appear in the file — so a linear workflow keeps
  exactly the sequence it has always had, and a branched one is deterministic
  rather than dependent on whichever branch happened to finish first.

Nothing here runs, validates, or draws anything.
:mod:`aura.agents.graph_validation` turns the same reading into the messages a
person sees, and :mod:`aura.agents.workflow_plan` freezes it into a plan.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from aura.agents.graph_models import ConnectionKind, WorkflowConnection, WorkflowGraph

#: One direction of the solid subgraph: a node id mapped to its neighbours in
#: the workflow's own persisted order, with repeats removed.
SolidLinks = Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class SolidStep:
    """One solid Agent occurrence and the neighbours it was drawn with.

    ``predecessors`` and ``successors`` name Agent occurrences only: the two
    fixed ends are not Steps, so the edges that touch them are recorded as
    ``from_task`` and ``to_result`` instead. A Step with several predecessors
    is a join and waits for all of them; one with several successors fans out
    to all of them.
    """

    node_id: str
    predecessors: tuple[str, ...] = ()
    successors: tuple[str, ...] = ()
    from_task: bool = False
    to_result: bool = False

    @property
    def is_join(self) -> bool:
        return len(self.predecessors) > 1

    @property
    def is_branch(self) -> bool:
        return len(self.successors) > 1


@dataclass(frozen=True)
class SolidDag:
    """One acyclic Task to Aura Result shape, in a settled order."""

    task_node_id: str
    result_node_id: str
    steps: tuple[SolidStep, ...] = ()

    def step(self, node_id: str) -> SolidStep | None:
        return next((item for item in self.steps if item.node_id == node_id), None)

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(step.node_id for step in self.steps)

    @property
    def entry_node_ids(self) -> tuple[str, ...]:
        """The Steps the workflow task reaches directly, in frozen order."""
        return tuple(step.node_id for step in self.steps if step.from_task)

    @property
    def terminal_node_ids(self) -> tuple[str, ...]:
        """The Steps that hand an answer to the Aura Result, in frozen order."""
        return tuple(step.node_id for step in self.steps if step.to_result)

    @property
    def branched(self) -> bool:
        """True when this workflow is anything other than one straight line."""
        return (
            any(step.is_join or step.is_branch for step in self.steps)
            or len(self.entry_node_ids) > 1
            or len(self.terminal_node_ids) > 1
        )


def solid_step_edges(graph: WorkflowGraph) -> tuple[WorkflowConnection, ...]:
    """Every solid line that could mean something, in persisted order.

    A line pointing at a node that is not on the canvas, or one joining a node
    to itself, describes no hand-off at all. Those are reported as issues by
    :mod:`aura.agents.graph_validation`; here they are simply not edges.
    """
    known = {node.node_id for node in graph.nodes}
    return tuple(
        edge
        for edge in sorted(
            graph.connections_of_kind(ConnectionKind.STEP), key=lambda item: item.order
        )
        if edge.source_id in known
        and edge.target_id in known
        and edge.source_id != edge.target_id
    )


def solid_links(
    edges: Iterable[WorkflowConnection],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Index *edges* both ways, keeping their order and dropping repeats.

    A file that draws the same hand-off twice still describes one hand-off, so
    a join never receives the same branch's result twice.
    """
    successors: dict[str, list[str]] = {}
    predecessors: dict[str, list[str]] = {}
    for edge in edges:
        forward = successors.setdefault(edge.source_id, [])
        if edge.target_id not in forward:
            forward.append(edge.target_id)
        backward = predecessors.setdefault(edge.target_id, [])
        if edge.source_id not in backward:
            backward.append(edge.source_id)
    return (
        {node_id: tuple(items) for node_id, items in successors.items()},
        {node_id: tuple(items) for node_id, items in predecessors.items()},
    )


def reachable(starts: Iterable[str], links: SolidLinks) -> frozenset[str]:
    """Every node *starts* can get to by following *links*, plus *starts*."""
    seen: set[str] = set()
    pending = list(starts)
    while pending:
        node_id = pending.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        pending.extend(links.get(node_id, ()))
    return frozenset(seen)


def topological_order(
    node_ids: tuple[str, ...], predecessors: SolidLinks
) -> tuple[str, ...] | None:
    """*node_ids* in dependency order, or ``None`` when they form a cycle.

    Ties are broken by the order the nodes appear on the canvas, which is the
    order they are written to the file — so the answer is the same on every
    machine that opens the same workflow, and a straight line comes back as
    exactly that line.
    """
    inside = set(node_ids)
    waiting = {
        node_id: {other for other in predecessors.get(node_id, ()) if other in inside}
        for node_id in node_ids
    }
    settled: set[str] = set()
    order: list[str] = []
    while len(order) < len(node_ids):
        for node_id in node_ids:
            if node_id not in settled and waiting[node_id] <= settled:
                settled.add(node_id)
                order.append(node_id)
                break
        else:
            return None
    return tuple(order)


def solid_dag(graph: WorkflowGraph) -> SolidDag | None:
    """The acyclic Task to Aura Result shape *graph* draws, or nothing.

    ``None`` means the solid lines are not one such shape — no fixed ends, a
    line running into the Task or out of the Aura Result, a cycle, or an Agent
    occurrence the Task cannot reach or that reaches no answer. A workflow
    whose shape cannot be read has no execution to describe, and inventing one
    would be the first step towards running the wrong thing.
    """
    task = graph.task_node
    result = graph.result_node
    if task is None or result is None:
        return None

    edges = solid_step_edges(graph)
    successors, predecessors = solid_links(edges)
    if predecessors.get(task.node_id) or successors.get(result.node_id):
        return None

    forward = reachable((task.node_id,), successors)
    backward = reachable((result.node_id,), predecessors)
    if result.node_id not in forward:
        return None

    by_id = {node.node_id: node for node in graph.nodes}
    touched = set(successors) | set(predecessors) | {task.node_id, result.node_id}
    on_path = tuple(node.node_id for node in graph.nodes if node.node_id in touched)
    for node_id in on_path:
        node = by_id[node_id]
        if node.is_agent and (node_id not in forward or node_id not in backward):
            return None

    order = topological_order(on_path, predecessors)
    if order is None:
        return None

    return SolidDag(
        task_node_id=task.node_id,
        result_node_id=result.node_id,
        steps=tuple(
            SolidStep(
                node_id=node_id,
                predecessors=tuple(
                    other
                    for other in predecessors.get(node_id, ())
                    if by_id[other].is_agent
                ),
                successors=tuple(
                    other
                    for other in successors.get(node_id, ())
                    if by_id[other].is_agent
                ),
                from_task=task.node_id in predecessors.get(node_id, ()),
                to_result=result.node_id in successors.get(node_id, ()),
            )
            for node_id in order
            if by_id[node_id].is_agent
        ),
    )


def runnable_dag(graph: WorkflowGraph) -> SolidDag | None:
    """The DAG a run would actually execute, or ``None`` when there is none.

    This is the one verdict the Run button and the toolbar switch ask about
    shape: a readable acyclic DAG with at least one Agent occurrence in it. A
    Task wired straight to the Aura Result is a complete drawing with nothing
    in it to do.
    """
    dag = solid_dag(graph)
    return dag if dag is not None and dag.steps else None


__all__ = [
    "SolidDag",
    "SolidLinks",
    "SolidStep",
    "reachable",
    "runnable_dag",
    "solid_dag",
    "solid_links",
    "solid_step_edges",
    "topological_order",
]
