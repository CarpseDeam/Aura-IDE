"""Exact saved Workflow facts and their model-facing authoring projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from aura.agents.graph_models import WorkflowGraph
from aura.agents.roster import AgentRosterEntry


@dataclass(frozen=True)
class WorkflowDocument:
    graph: WorkflowGraph
    agents: tuple[AgentRosterEntry, ...]

    @property
    def revision(self) -> str:
        # Model/instructions/grant edits matter as much as graph edits.
        value = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def payload(self) -> dict:
        graph = self.graph
        aliases = {
            node.node_id: node.node_id if node.is_agent else ("task" if node == graph.task_node else "result")
            for node in graph.nodes
        }
        return {
            "workflow_id": graph.graph_id,
            "revision": self.revision,
            "scope": graph.scope.value,
            "name": graph.name,
            "description": graph.description,
            "new_agents": [],
            "occurrences": [
                {"alias": node.node_id, "agent_ref": node.agent_id, "assignment": node.assignment}
                for node in graph.nodes
                if node.is_agent
            ],
            "handoffs": [
                {"source": aliases[edge.source_id], "target": aliases[edge.target_id]}
                for edge in graph.connections
                if edge.is_step
            ],
            "helpers": [
                {"parent": aliases[edge.source_id], "helper": aliases[edge.target_id]}
                for edge in graph.connections
                if not edge.is_step
            ],
            "agents": [{**asdict(entry.definition), "permission": entry.permission.value} for entry in self.agents],
        }


@dataclass(frozen=True)
class WorkflowSaved:
    """A completed authoring action, suitable for a transient chat card."""

    document: WorkflowDocument
    status: str
    can_undo: bool
