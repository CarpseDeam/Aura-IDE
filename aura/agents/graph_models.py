"""The shape of one Agent workflow graph.

A graph is a *drawing of intent*: which agents take part, what each of them
is asked to do in this particular workflow, and in what order the work
moves. It is deliberately not a copy of those agents. A node carries an
immutable agent id and the assignment written for that one occurrence —
never the agent's instructions, model target, or thinking mode, all of which
stay in the definition (:mod:`aura.agents.models`) and are read through it.

Three identities matter, and none of them is a name:

* the **graph id** names the file, so it is minted once and never derived
  from a title the user can retype;
* the **node id** is one occurrence on one canvas, so the same agent may
  appear twice with two different assignments and stay two separate things;
* the **connection id** is one line, so its manual routing survives a
  reconnection at either end.

Nothing here is Qt-aware, and nothing here decides whether a workflow may be
run — that is :mod:`aura.agents.graph_validation` — or whether Aura may call
it, which is private local state in :mod:`aura.agents.graph_local_state`.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from enum import Enum

from aura.agents.identity import AgentScope

#: A graph id is used verbatim as a file name stem, so it carries no path
#: separator, drive letter, leading dot, or anything else that would let a
#: workflow name a location instead of itself.
_GRAPH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")

#: Node and connection ids never touch the filesystem, so they only have to
#: be opaque, non-empty, and free of whitespace.
_PART_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class WorkflowNodeKind(str, Enum):
    """What one box on the canvas is.

    ``TASK`` and ``AURA_RESULT`` are the two fixed ends of a workflow: the
    work that arrives, and the answer that goes back to Aura. Exactly one of
    each exists, neither is created or deleted by the user, and neither
    references an agent. Everything between them is an ``AGENT`` occurrence.
    """

    TASK = "task"
    AGENT = "agent"
    AURA_RESULT = "aura_result"

    @property
    def is_fixed(self) -> bool:
        return self is not WorkflowNodeKind.AGENT

    @property
    def label(self) -> str:
        return _NODE_KIND_LABELS[self]

    @classmethod
    def parse(cls, raw: object) -> "WorkflowNodeKind | None":
        if isinstance(raw, WorkflowNodeKind):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


_NODE_KIND_LABELS: dict[WorkflowNodeKind, str] = {
    WorkflowNodeKind.TASK: "Task",
    WorkflowNodeKind.AGENT: "Agent",
    WorkflowNodeKind.AURA_RESULT: "Aura Result",
}


class ConnectionKind(str, Enum):
    """What one line between two boxes means.

    ``STEP`` is the automatic hand-off drawn solid: when this node finishes,
    the next one starts. ``SUB_AGENT`` is drawn dashed and labelled, and says
    only that a helper is available to the node it hangs from. The two are
    never interchangeable, which is why the canvas has a port for each rather
    than one port and a dropdown.
    """

    STEP = "step"
    SUB_AGENT = "sub_agent"

    @property
    def label(self) -> str:
        return "Next step" if self is ConnectionKind.STEP else "Sub-agent"

    @classmethod
    def parse(cls, raw: object) -> "ConnectionKind | None":
        if isinstance(raw, ConnectionKind):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


#: How precisely a canvas coordinate is kept. Two decimals is far finer than
#: anything a person can aim at, and it is exactly what a workflow file
#: carries — so rounding here, at the model, rather than only in the writer
#: keeps the workflow in memory byte-identical to the one on disk. A reload
#: then never nudges a node, and never manufactures an undo step out of a
#: rounding difference nobody made.
COORDINATE_PLACES = 2


@dataclass(frozen=True)
class Point:
    """A canvas coordinate, or an offset from one."""

    x: float = 0.0
    y: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", round(float(self.x), COORDINATE_PLACES))
        object.__setattr__(self, "y", round(float(self.y), COORDINATE_PLACES))

    def shifted(self, dx: float, dy: float) -> "Point":
        return Point(self.x + dx, self.y + dy)


@dataclass(frozen=True)
class WorkflowNode:
    """One box: an occurrence, not an agent.

    ``agent_id`` is empty on the two fixed nodes. ``assignment`` is what this
    occurrence is asked to do *here* — it belongs to the workflow, so the
    same agent placed twice answers two different briefs.
    """

    node_id: str
    kind: WorkflowNodeKind
    position: Point = Point()
    agent_id: str = ""
    assignment: str = ""

    @property
    def is_agent(self) -> bool:
        return self.kind is WorkflowNodeKind.AGENT

    def moved_to(self, x: float, y: float) -> "WorkflowNode":
        return replace(self, position=Point(float(x), float(y)))


@dataclass(frozen=True)
class WorkflowConnection:
    """One line, its kind, its place in the order, and its manual routing.

    ``bend`` is an offset from the curve's own resting midpoint rather than a
    scene coordinate, so a routing the user shaped by hand keeps its shape
    when either end is dragged somewhere else. ``None`` means never touched.
    """

    connection_id: str
    kind: ConnectionKind
    source_id: str
    target_id: str
    order: int = 0
    bend: Point | None = None

    @property
    def is_step(self) -> bool:
        return self.kind is ConnectionKind.STEP

    def rerouted(self, bend: Point | None) -> "WorkflowConnection":
        return replace(self, bend=bend)

    def reconnected(
        self, *, source_id: str = "", target_id: str = ""
    ) -> "WorkflowConnection":
        return replace(
            self,
            source_id=source_id or self.source_id,
            target_id=target_id or self.target_id,
        )


@dataclass(frozen=True)
class WorkflowGraph:
    """One workflow, exactly as its JSON file describes it.

    Scope is read off the directory the file was found in, the same way an
    agent definition's is, so moving a workflow between the project and the
    personal library is the whole act of changing its scope.
    """

    graph_id: str
    scope: AgentScope
    name: str
    description: str = ""
    nodes: tuple[WorkflowNode, ...] = ()
    connections: tuple[WorkflowConnection, ...] = ()

    # ---- reading -----------------------------------------------------------

    def node(self, node_id: str) -> WorkflowNode | None:
        return next((item for item in self.nodes if item.node_id == node_id), None)

    def connection(self, connection_id: str) -> WorkflowConnection | None:
        return next(
            (item for item in self.connections if item.connection_id == connection_id),
            None,
        )

    def nodes_of_kind(self, kind: WorkflowNodeKind) -> tuple[WorkflowNode, ...]:
        return tuple(item for item in self.nodes if item.kind is kind)

    def connections_of_kind(
        self, kind: ConnectionKind
    ) -> tuple[WorkflowConnection, ...]:
        return tuple(item for item in self.connections if item.kind is kind)

    @property
    def task_node(self) -> WorkflowNode | None:
        found = self.nodes_of_kind(WorkflowNodeKind.TASK)
        return found[0] if len(found) == 1 else None

    @property
    def result_node(self) -> WorkflowNode | None:
        found = self.nodes_of_kind(WorkflowNodeKind.AURA_RESULT)
        return found[0] if len(found) == 1 else None

    @property
    def agent_ids(self) -> tuple[str, ...]:
        """Every distinct agent this workflow refers to, in placement order."""
        seen: list[str] = []
        for item in self.nodes:
            if item.is_agent and item.agent_id and item.agent_id not in seen:
                seen.append(item.agent_id)
        return tuple(seen)

    def outgoing(
        self, node_id: str, kind: ConnectionKind
    ) -> tuple[WorkflowConnection, ...]:
        return tuple(
            item
            for item in sorted(self.connections, key=lambda edge: edge.order)
            if item.kind is kind and item.source_id == node_id
        )

    def incoming(
        self, node_id: str, kind: ConnectionKind
    ) -> tuple[WorkflowConnection, ...]:
        return tuple(
            item
            for item in sorted(self.connections, key=lambda edge: edge.order)
            if item.kind is kind and item.target_id == node_id
        )

    # ---- writing -----------------------------------------------------------

    def with_name(self, name: str, description: str | None = None) -> "WorkflowGraph":
        return replace(
            self,
            name=str(name or "").strip(),
            description=(
                self.description
                if description is None
                else str(description or "").strip()
            ),
        )

    def with_node(self, node: WorkflowNode) -> "WorkflowGraph":
        """Add *node*, or replace the one that already carries its id."""
        if self.node(node.node_id) is None:
            return replace(self, nodes=(*self.nodes, node))
        return replace(
            self,
            nodes=tuple(
                node if item.node_id == node.node_id else item for item in self.nodes
            ),
        )

    def without_node(self, node_id: str) -> "WorkflowGraph":
        """Remove one occurrence and every line that touched it.

        Only the occurrence goes: the agent definition it referenced is a
        separate, reusable thing and is never affected by a canvas deletion.
        """
        return replace(
            self,
            nodes=tuple(item for item in self.nodes if item.node_id != node_id),
            connections=tuple(
                item
                for item in self.connections
                if node_id not in (item.source_id, item.target_id)
            ),
        )

    def with_connection(self, connection: WorkflowConnection) -> "WorkflowGraph":
        if self.connection(connection.connection_id) is None:
            return replace(self, connections=(*self.connections, connection))
        return replace(
            self,
            connections=tuple(
                connection if item.connection_id == connection.connection_id else item
                for item in self.connections
            ),
        )

    def without_connection(self, connection_id: str) -> "WorkflowGraph":
        return replace(
            self,
            connections=tuple(
                item
                for item in self.connections
                if item.connection_id != connection_id
            ),
        )

    def next_order(self) -> int:
        return max((item.order for item in self.connections), default=-1) + 1


# ---- identity --------------------------------------------------------------


def new_graph_id() -> str:
    """Mint an id for a brand-new workflow — opaque, so renaming is only that."""
    return uuid.uuid4().hex


def new_node_id() -> str:
    """Mint an id for one canvas occurrence."""
    return f"n{uuid.uuid4().hex[:16]}"


def new_connection_id() -> str:
    """Mint an id for one line."""
    return f"c{uuid.uuid4().hex[:16]}"


def is_valid_graph_id(raw: object) -> bool:
    """True for an id that is safe to use as a workflow's file name."""
    return isinstance(raw, str) and bool(_GRAPH_ID_RE.match(raw))


def is_valid_part_id(raw: object) -> bool:
    """True for a node or connection id that can be stored and addressed."""
    return isinstance(raw, str) and bool(_PART_ID_RE.match(raw))


# ---- starting layout -------------------------------------------------------

#: Where the two fixed nodes sit in a workflow that was just created. Far
#: enough apart that the first agent dropped between them has somewhere to
#: land without overlapping either end.
DEFAULT_TASK_POSITION = Point(-320.0, 0.0)
DEFAULT_RESULT_POSITION = Point(320.0, 0.0)


def new_graph(scope: AgentScope, name: str, *, description: str = "") -> WorkflowGraph:
    """A brand-new workflow: the two fixed ends, joined by nothing yet."""
    return WorkflowGraph(
        graph_id=new_graph_id(),
        scope=scope,
        name=str(name or "").strip(),
        description=str(description or "").strip(),
        nodes=(
            WorkflowNode(
                node_id=new_node_id(),
                kind=WorkflowNodeKind.TASK,
                position=DEFAULT_TASK_POSITION,
            ),
            WorkflowNode(
                node_id=new_node_id(),
                kind=WorkflowNodeKind.AURA_RESULT,
                position=DEFAULT_RESULT_POSITION,
            ),
        ),
    )


__all__ = [
    "COORDINATE_PLACES",
    "DEFAULT_RESULT_POSITION",
    "DEFAULT_TASK_POSITION",
    "AgentScope",
    "ConnectionKind",
    "Point",
    "WorkflowConnection",
    "WorkflowGraph",
    "WorkflowNode",
    "WorkflowNodeKind",
    "is_valid_graph_id",
    "is_valid_part_id",
    "new_connection_id",
    "new_graph",
    "new_graph_id",
    "new_node_id",
]
