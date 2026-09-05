"""Qt-free retention of exact automatically compiled Agents and Workflows.

The compiler already owns the generated definitions and native graph. This
module validates those immutable objects, extracts their exact grants, checks
every identity before writing, and coordinates Aura's existing stores. It does
not reconstruct anything from chat presentation and does not invent a Team
persistence format.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from aura.agents.graph_dag import runnable_dag
from aura.agents.graph_models import WorkflowGraph
from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError
from aura.agents.graph_validation import validate_graph
from aura.agents.identity import AgentScope
from aura.agents.local_state import (
    DEFAULT_PERMISSION,
    AgentLocalState,
    AgentLocalStateError,
    AgentPermission,
)
from aura.agents.models import AgentDefinition
from aura.agents.store import AgentStore, AgentStoreError
from aura.agents.team_compiler import CompiledAgentTeam
from aura.agents.workflow_builder import BuiltWorkflow


class AgentRetentionError(RuntimeError):
    """An exact compiled artifact could not be safely retained."""


@dataclass(frozen=True)
class AgentRetentionResult:
    """A completed durable retention action."""

    message: str
    agent_ids: tuple[str, ...] = ()
    workflow_id: str = ""


@dataclass(frozen=True)
class CompiledTeamFacts:
    """Validated, deduplicated facts extracted from one compiled team."""

    graph: WorkflowGraph
    generated_definitions: tuple[AgentDefinition, ...]
    definitions: Mapping[str, AgentDefinition]
    permissions: Mapping[str, AgentPermission]


def validate_compiled_team(team: CompiledAgentTeam) -> CompiledTeamFacts:
    """Validate retained objects and extract exact definition/grant maps."""
    if not isinstance(team, CompiledAgentTeam):
        raise AgentRetentionError("The Agent run no longer has a compiled team.")
    plan = team.plan
    graph = plan.graph
    if graph.scope is not AgentScope.PERSONAL:
        raise AgentRetentionError("An automatic run can only be kept as a personal Workflow.")
    if (
        plan.graph_id != graph.graph_id
        or plan.name != graph.name
        or plan.description != graph.description
    ):
        raise AgentRetentionError("The compiled Workflow identity is inconsistent.")

    definitions: dict[str, AgentDefinition] = {}
    permissions: dict[str, AgentPermission] = {}
    node_facts: dict[str, tuple[str, str]] = {}
    for step in plan.steps:
        occurrences = (step, *(item for root in step.helpers for item in root.preorder()))
        for occurrence in occurrences:
            agent_id = occurrence.agent_id
            definition = occurrence.definition
            permission = AgentPermission(occurrence.permission)
            if agent_id in definitions and definitions[agent_id] != definition:
                raise AgentRetentionError(
                    f"Agent id {agent_id} has conflicting compiled definitions."
                )
            if agent_id in permissions and permissions[agent_id] is not permission:
                raise AgentRetentionError(
                    f"Agent id {agent_id} has conflicting compiled permissions."
                )
            definitions[agent_id] = definition
            permissions[agent_id] = permission
            node_facts[occurrence.node_id] = (
                agent_id,
                str(occurrence.assignment or "").strip(),
            )

    graph_facts = {
        node.node_id: (node.agent_id, str(node.assignment or "").strip())
        for node in graph.nodes
        if node.is_agent
    }
    if graph_facts != node_facts:
        raise AgentRetentionError("The compiled plan no longer matches its exact graph.")
    scopes = {agent_id: definition.scope for agent_id, definition in definitions.items()}
    verdict = validate_graph(graph, agents=scopes)
    if not verdict.runnable or runnable_dag(graph) is None:
        reason = "; ".join(verdict.messages) or "the graph is not runnable"
        raise AgentRetentionError(f"The compiled Workflow cannot be retained: {reason}.")

    generated: dict[str, AgentDefinition] = {}
    generated_order: list[str] = []
    for definition in team.generated_definitions:
        if not isinstance(definition, AgentDefinition):
            raise AgentRetentionError("A generated Agent definition is invalid.")
        if definition.scope is not AgentScope.PERSONAL:
            raise AgentRetentionError("Generated Agents can only be retained as personal Agents.")
        existing = generated.get(definition.agent_id)
        if existing is not None and existing != definition:
            raise AgentRetentionError(
                f"Generated Agent id {definition.agent_id} has conflicting content."
            )
        if existing is None:
            generated[definition.agent_id] = definition
            generated_order.append(definition.agent_id)
        if definitions.get(definition.agent_id) != definition:
            raise AgentRetentionError(
                f"Generated Agent id {definition.agent_id} does not match the compiled plan."
            )
    if set(generated) - set(graph.agent_ids):
        raise AgentRetentionError("The compiled team contains an unused generated Agent.")

    return CompiledTeamFacts(
        graph=graph,
        generated_definitions=tuple(generated[agent_id] for agent_id in generated_order),
        definitions=MappingProxyType(definitions),
        permissions=MappingProxyType(permissions),
    )


class AgentTeamRetention:
    """Coordinate retryable retention through Aura's existing persistence."""

    def __init__(
        self,
        *,
        agents: AgentStore,
        workflows: AgentGraphStore,
        local_state: AgentLocalState,
    ) -> None:
        self._agents = agents
        self._workflows = workflows
        self._local_state = local_state

    def save_agent(
        self, team: CompiledAgentTeam, agent_id: str
    ) -> AgentRetentionResult:
        facts = validate_compiled_team(team)
        definition = next(
            (
                item
                for item in facts.generated_definitions
                if item.agent_id == str(agent_id or "").strip()
            ),
            None,
        )
        if definition is None:
            raise AgentRetentionError("That generated Agent is not part of this run.")
        existing_generated = self._preflight_generated((definition,))
        self._preflight_permissions(
            (
                {definition.agent_id: facts.permissions[definition.agent_id]}
                if existing_generated
                else {}
            ),
            definitions=facts.definitions,
            allow_unrecorded=existing_generated,
        )
        try:
            self._agents.create_supplied(definition)
            self._local_state.retain_available_agent(
                definition.agent_id, facts.permissions[definition.agent_id]
            )
        except (AgentStoreError, AgentLocalStateError) as exc:
            raise AgentRetentionError(str(exc)) from exc
        return AgentRetentionResult(
            message="Saved",
            agent_ids=(definition.agent_id,),
        )

    def keep_team(self, team: CompiledAgentTeam) -> AgentRetentionResult:
        return self._keep_facts(validate_compiled_team(team), self._workflows.create_supplied)

    def save_workflow(
        self, built: BuiltWorkflow, commit: Callable[[WorkflowGraph], object]
    ) -> AgentRetentionResult:
        """Persist authoring facts through the same identity and grant checks."""
        facts = CompiledTeamFacts(
            built.graph, built.generated_definitions, built.definitions, built.permissions
        )
        return self._keep_facts(facts, commit, replacing=True)

    def _keep_facts(
        self, facts: CompiledTeamFacts, commit: Callable[[WorkflowGraph], object],
        *, replacing: bool = False,
    ) -> AgentRetentionResult:
        generated_ids = {item.agent_id for item in facts.generated_definitions}
        existing_generated = self._preflight_generated(facts.generated_definitions)
        reused_definitions = {
            agent_id: definition
            for agent_id, definition in facts.definitions.items()
            if agent_id not in generated_ids
        }
        self._preflight_existing(reused_definitions)
        self._preflight_permissions(
            {
                agent_id: permission
                for agent_id, permission in facts.permissions.items()
                if agent_id in reused_definitions or agent_id in existing_generated
            },
            definitions=facts.definitions,
            allow_unrecorded=existing_generated,
        )
        if not replacing:
            self._preflight_workflow(facts.graph)

        try:
            for definition in facts.generated_definitions:
                self._agents.create_supplied(definition)
            self._local_state.set_permissions(
                {
                    definition.agent_id: facts.permissions[definition.agent_id]
                    for definition in facts.generated_definitions
                }
            )
            commit(facts.graph)
        except (AgentStoreError, AgentGraphStoreError, AgentLocalStateError) as exc:
            raise AgentRetentionError(str(exc)) from exc
        return AgentRetentionResult(
            message="Kept",
            agent_ids=tuple(item.agent_id for item in facts.generated_definitions),
            workflow_id=facts.graph.graph_id,
        )

    def _preflight_generated(
        self, definitions: tuple[AgentDefinition, ...]
    ) -> frozenset[str]:
        try:
            rows = self._agents.list_summaries()
        except Exception as exc:
            raise AgentRetentionError(f"Could not inspect saved Agents: {exc}") from exc
        existing_ids: set[str] = set()
        for definition in definitions:
            matches = [row for row in rows if row.agent_id == definition.agent_id]
            if not matches:
                continue
            if (
                len(matches) == 1
                and matches[0].scope is definition.scope
                and matches[0].definition == definition
            ):
                existing_ids.add(definition.agent_id)
                continue
            raise AgentRetentionError(
                f"Agent id {definition.agent_id} already exists with different content."
            )
        return frozenset(existing_ids)

    def _preflight_existing(
        self, definitions: Mapping[str, AgentDefinition]
    ) -> None:
        try:
            rows = self._agents.list_summaries()
        except Exception as exc:
            raise AgentRetentionError(f"Could not inspect saved Agents: {exc}") from exc
        by_id = {row.agent_id: row for row in rows}
        for agent_id, definition in sorted(definitions.items()):
            row = by_id.get(agent_id)
            if row is None:
                raise AgentRetentionError(
                    f"Saved Agent id {agent_id} is no longer available; the team was not kept."
                )
            if row.definition != definition:
                raise AgentRetentionError(
                    f"Saved Agent id {agent_id} changed after this run; the team was not kept."
                )

    def _preflight_permissions(
        self,
        permissions: Mapping[str, AgentPermission],
        *,
        definitions: Mapping[str, AgentDefinition],
        allow_unrecorded: frozenset[str] = frozenset(),
    ) -> None:
        """Refuse a stale grant without blocking an interrupted-save retry."""
        for agent_id, expected in sorted(permissions.items()):
            try:
                explicit = self._local_state.explicit_permission(agent_id)
            except AgentLocalStateError as exc:
                raise AgentRetentionError(str(exc)) from exc
            current = explicit or DEFAULT_PERMISSION
            if current is expected:
                continue
            if agent_id in allow_unrecorded and explicit is None:
                # The immutable definition exists but its grant does not. This
                # is the only partial state an earlier exact save may repair.
                continue
            definition = definitions[agent_id]
            raise AgentRetentionError(
                f'Agent "{definition.name}": permission changed after this run '
                f"from {expected.label} to {current.label}. Aura did not overwrite "
                "the newer grant."
            )

    def _preflight_workflow(self, graph: WorkflowGraph) -> None:
        try:
            matches = [
                row
                for row in self._workflows.list_summaries()
                if row.graph_id == graph.graph_id
            ]
        except Exception as exc:
            raise AgentRetentionError(f"Could not inspect saved Workflows: {exc}") from exc
        if not matches:
            return
        if (
            len(matches) == 1
            and matches[0].scope is graph.scope
            and matches[0].graph == graph
        ):
            return
        raise AgentRetentionError(
            f"Workflow id {graph.graph_id} already exists with different content."
        )


__all__ = [
    "AgentRetentionError",
    "AgentRetentionResult",
    "AgentTeamRetention",
    "CompiledTeamFacts",
    "validate_compiled_team",
]
