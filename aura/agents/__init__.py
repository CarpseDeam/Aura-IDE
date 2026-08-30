"""Aura's Agents: named helpers with their own brief, model, and authority.

This package owns two separate things and keeps them apart on purpose.

* A **definition** (:mod:`aura.agents.store`, :mod:`aura.agents.models`) is
  what an agent is — its immutable id, name, delegation description,
  instructions, model target, and thinking mode. It is human-readable
  Markdown on disk, and it lives either in the project or in the user's own
  data directory.
* **Local state** (:mod:`aura.agents.local_state`) is what one user, in one
  workspace, has decided about those definitions — which are available to
  Aura, in what order, and what each is allowed to do. It is private and
  never travels with the project.

* **Execution** (:mod:`aura.agents.roster`, :mod:`aura.agents.runtime`,
  :mod:`aura.agents.delegation`) is what happens when Aura hands one of them
  a task: a roster and grant frozen for the length of a turn, one foreground
  child run, and the single structured result that comes back. Writable runs
  are isolated and retained by :mod:`aura.agents.worktree`.

Nothing here is Qt-aware.
"""
from __future__ import annotations

from aura.agents.delegation import (
    DelegationFailure,
    DelegationResult,
    DelegationStatus,
    DelegationUsage,
)
from aura.agents.graph_document import parse_graph_document, render_graph_document
from aura.agents.graph_history import GraphHistory
from aura.agents.graph_local_state import WorkflowLocalState, WorkflowLocalStateError
from aura.agents.graph_models import (
    ConnectionKind,
    Point,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
    is_valid_graph_id,
    new_graph,
    new_graph_id,
)
from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError, WorkflowSummary
from aura.agents.graph_validation import (
    GraphIssue,
    GraphValidation,
    validate_graph,
)
from aura.agents.identity import SCOPE_ORDER, AgentScope, is_valid_agent_id, new_agent_id
from aura.agents.local_state import (
    DEFAULT_PERMISSION,
    PERMISSION_ORDER,
    TERMINAL_WARNING,
    AgentLocalState,
    AgentLocalStateError,
    AgentPermission,
)
from aura.agents.models import THINKING_ORDER, AgentDefinition, AgentThinking, ModelTarget
from aura.agents.roster import (
    EMPTY_AGENT_ROSTER,
    AgentRosterEntry,
    AgentTurnRoster,
    resolve_agent_turn_roster,
)
from aura.agents.store import AgentStore, AgentStoreError, AgentSummary
from aura.agents.worktree import (
    AgentChangeSet,
    AgentWorktree,
    AgentWorktreeError,
    AgentWorktreeManager,
)

__all__ = [
    "DEFAULT_PERMISSION",
    "EMPTY_AGENT_ROSTER",
    "PERMISSION_ORDER",
    "SCOPE_ORDER",
    "TERMINAL_WARNING",
    "THINKING_ORDER",
    "AgentDefinition",
    "AgentChangeSet",
    "AgentGraphStore",
    "AgentGraphStoreError",
    "AgentLocalState",
    "AgentLocalStateError",
    "AgentPermission",
    "AgentRosterEntry",
    "AgentScope",
    "AgentStore",
    "AgentStoreError",
    "AgentSummary",
    "AgentThinking",
    "AgentTurnRoster",
    "AgentWorktree",
    "AgentWorktreeError",
    "AgentWorktreeManager",
    "ConnectionKind",
    "DelegationFailure",
    "DelegationResult",
    "DelegationStatus",
    "DelegationUsage",
    "GraphHistory",
    "GraphIssue",
    "GraphValidation",
    "ModelTarget",
    "Point",
    "WorkflowConnection",
    "WorkflowGraph",
    "WorkflowLocalState",
    "WorkflowLocalStateError",
    "WorkflowNode",
    "WorkflowNodeKind",
    "WorkflowSummary",
    "is_valid_agent_id",
    "is_valid_graph_id",
    "new_agent_id",
    "new_graph",
    "new_graph_id",
    "parse_graph_document",
    "render_graph_document",
    "resolve_agent_turn_roster",
    "validate_graph",
]
