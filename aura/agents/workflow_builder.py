"""Pure semantic construction shared by authoring and automatic teams."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping, Protocol

from aura.agents.graph_models import (
    ConnectionKind,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
    new_connection_id,
    new_graph,
    new_node_id,
)
from aura.agents.graph_validation import GraphIssue, validate_graph
from aura.agents.identity import AgentScope, new_agent_id
from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentDefinition, AgentThinking
from aura.agents.roster import AgentTurnRoster
from aura.agents.team_spec import (
    INHERIT_MODEL_TARGET_KEY,
    MAX_AGENT_TEAM_OCCURRENCES,
    NewAgentSpec,
    WorkflowSpec,
)
from aura.agents.validation import (
    agent_name_error,
    delegation_description_error,
    workflow_name_error,
)
from aura.agents.workflow_layout import layout_workflow

_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_RESERVED_ALIASES = frozenset({"task", "result"})


class _ModelTarget(Protocol):
    """The two frozen target facts the compiler consumes."""

    provider: str
    model: str


class FrozenModelTargetLookup(Protocol):
    """A frozen target catalog, normally from the submitted turn context."""

    def get(self, key: str) -> _ModelTarget | None: ...


@dataclass(frozen=True)
class BuiltWorkflow:
    """Validated native authoring facts; no resolved run or execution task."""

    graph: WorkflowGraph
    generated_definitions: tuple[AgentDefinition, ...]
    definitions: Mapping[str, AgentDefinition]
    permissions: Mapping[str, AgentPermission]


@dataclass(frozen=True)
class _NewAgentFacts:
    spec: NewAgentSpec
    target: _ModelTarget
    thinking: AgentThinking
    permission: AgentPermission


def build_workflow(
    spec: WorkflowSpec,
    *,
    roster: AgentTurnRoster,
    model_targets: FrozenModelTargetLookup,
    base: WorkflowGraph | None = None,
) -> tuple[BuiltWorkflow | None, tuple[str, ...]]:
    """Construct or revise a complete native graph without executing it.

    Existing occurrence aliases are their node ids. Unchanged nodes and
    connections retain their identity and user-shaped layout on revision.
    """
    existing = {entry.agent_id: entry for entry in roster.entries}
    facts, errors = _validate_semantics(spec, existing, model_targets)
    if errors:
        return None, errors

    definitions = {agent_id: entry.definition for agent_id, entry in existing.items()}
    permissions = {agent_id: entry.permission for agent_id, entry in existing.items()}
    scopes = {agent_id: entry.definition.scope for agent_id, entry in existing.items()}
    generated: list[AgentDefinition] = []
    new_ids: dict[str, str] = {}
    for item in spec.new_agents:
        alias = item.alias.strip()
        resolved = facts[alias]
        target_key = item.model_target.strip()
        explicit_target = target_key != INHERIT_MODEL_TARGET_KEY
        agent_id = new_agent_id()
        if agent_id in definitions:
            return None, ("could not mint a unique id for a generated Agent",)
        definition = AgentDefinition(
            agent_id=agent_id,
            scope=base.scope if base is not None else AgentScope.PERSONAL,
            name=item.name.strip(),
            description=item.description.strip(),
            instructions=item.instructions.strip(),
            provider=(resolved.target.provider.strip() if explicit_target else ""),
            model=(resolved.target.model.strip() if explicit_target else ""),
            thinking=resolved.thinking,
        )
        new_ids[alias] = agent_id
        definitions[agent_id] = definition
        permissions[agent_id] = resolved.permission
        scopes[agent_id] = definition.scope
        generated.append(definition)

    graph, node_aliases, connection_paths, graph_errors = _build_graph(
        spec,
        existing_ids=frozenset(existing),
        new_ids=new_ids,
        base=base,
    )
    if graph_errors:
        return None, graph_errors
    assert graph is not None

    verdict = validate_graph(graph, agents=scopes)
    if not verdict.runnable:
        return None, tuple(_graph_issue_message(issue, node_aliases, connection_paths) for issue in verdict.issues)

    laid_out = layout_workflow(graph)
    if base is not None:
        # Retain the exact manual layout of every surviving occurrence.
        retained = {node.node_id: node.position for node in base.nodes}
        occupied = [retained[node.node_id] for node in laid_out.nodes if node.node_id in retained]
        nodes = []
        for node in laid_out.nodes:
            if node.node_id in retained:
                node = replace(node, position=retained[node.node_id])
            else:
                # Place additions clear of existing manually positioned nodes.
                while any(
                    abs(node.position.x - point.x) < 250 and abs(node.position.y - point.y) < 150 for point in occupied
                ):
                    node = node.moved_to(node.position.x, node.position.y + 180)
                occupied.append(node.position)
            nodes.append(node)
        laid_out = replace(laid_out, nodes=tuple(nodes))
    return BuiltWorkflow(
        laid_out,
        tuple(generated),
        MappingProxyType({key: value for key, value in definitions.items() if key in graph.agent_ids}),
        MappingProxyType({key: value for key, value in permissions.items() if key in graph.agent_ids}),
    ), ()


def _validate_semantics(
    spec: WorkflowSpec,
    existing: dict[str, object],
    model_targets: FrozenModelTargetLookup,
) -> tuple[dict[str, _NewAgentFacts], tuple[str, ...]]:
    errors: list[str] = []
    name_error = workflow_name_error(spec.name)
    if name_error:
        errors.append(name_error)

    occurrence_count = len(spec.occurrences)
    if not 1 <= occurrence_count <= MAX_AGENT_TEAM_OCCURRENCES:
        errors.append(
            "an automatically assembled team needs between 1 and "
            f"{MAX_AGENT_TEAM_OCCURRENCES} Agent occurrences, including helpers; "
            f"this one has {occurrence_count}"
        )

    new_by_alias: dict[str, NewAgentSpec] = {}
    facts: dict[str, _NewAgentFacts] = {}
    for index, item in enumerate(spec.new_agents):
        path = f"new_agents[{index}]"
        alias = item.alias.strip()
        _check_alias(alias, f"{path}.alias", errors)
        if alias in new_by_alias:
            errors.append(f"{path}.alias duplicates the new Agent alias '{alias}'")
            continue
        if alias in existing:
            errors.append(f"{path}.alias '{alias}' is ambiguous with an existing Agent id")
        new_by_alias[alias] = item

        agent_error = agent_name_error(item.name)
        if agent_error:
            errors.append(f"{path}.name: {agent_error}")
        description_error = delegation_description_error(item.description)
        if description_error:
            errors.append(f"{path}.description: {description_error}")
        if not item.instructions.strip():
            errors.append(f"{path}.instructions: an Agent needs instructions")

        parsed_thinking = AgentThinking.parse(item.thinking)
        if parsed_thinking is None:
            errors.append(f"{path}.thinking must be one of: inherit, off, high, max")
        try:
            parsed_permission = AgentPermission(item.permission.strip().lower())
        except (AttributeError, ValueError):
            parsed_permission = None
            errors.append(f"{path}.permission must be one of: read_only, read_write")

        target_key = item.model_target.strip()
        try:
            target = model_targets.get(target_key)
        except Exception:
            target = None
        if target is None:
            errors.append(f"{path}.model_target '{target_key}' is not available on this turn")
        elif target_key != INHERIT_MODEL_TARGET_KEY and (
            not str(target.provider or "").strip() or not str(target.model or "").strip()
        ):
            errors.append(f"{path}.model_target '{target_key}' does not resolve to a complete provider and model")

        if target is not None and parsed_thinking is not None and parsed_permission is not None:
            facts[alias] = _NewAgentFacts(
                spec=item,
                target=target,
                thinking=parsed_thinking,
                permission=parsed_permission,
            )

    occurrence_aliases: set[str] = set()
    used_new_agents: set[str] = set()
    known_refs = set(existing) | set(new_by_alias)
    for index, item in enumerate(spec.occurrences):
        path = f"occurrences[{index}]"
        alias = item.alias.strip()
        _check_alias(alias, f"{path}.alias", errors)
        if alias in occurrence_aliases:
            errors.append(f"{path}.alias duplicates the occurrence alias '{alias}'")
        occurrence_aliases.add(alias)
        agent_ref = item.agent_ref.strip()
        if agent_ref not in known_refs:
            errors.append(
                f"{path}.agent_ref '{agent_ref}' is not a declared new Agent or an Agent on this turn's frozen roster"
            )
        elif agent_ref in new_by_alias:
            used_new_agents.add(agent_ref)
        if not item.assignment.strip():
            errors.append(f"{path}.assignment must describe this Agent's work")

    for alias in new_by_alias:
        if alias not in used_new_agents:
            errors.append(f"new Agent '{alias}' is never used by an occurrence")

    seen_handoffs: set[tuple[str, str]] = set()
    for index, item in enumerate(spec.handoffs):
        path = f"handoffs[{index}]"
        source = item.source.strip()
        target = item.target.strip()
        if source == "result":
            errors.append(f"{path}.source cannot be result")
        elif source != "task" and source not in occurrence_aliases:
            errors.append(f"{path}.source '{source}' is not an occurrence or task")
        if target == "task":
            errors.append(f"{path}.target cannot be task")
        elif target != "result" and target not in occurrence_aliases:
            errors.append(f"{path}.target '{target}' is not an occurrence or result")
        edge = (source, target)
        if edge in seen_handoffs:
            errors.append(f"{path} duplicates the handoff {source} -> {target}")
        seen_handoffs.add(edge)
        if source == target:
            errors.append(f"{path} cannot connect '{source}' to itself")

    seen_helpers: set[tuple[str, str]] = set()
    for index, item in enumerate(spec.helpers):
        path = f"helpers[{index}]"
        parent = item.parent.strip()
        helper = item.helper.strip()
        if parent not in occurrence_aliases:
            errors.append(f"{path}.parent '{parent}' is not an occurrence")
        if helper not in occurrence_aliases:
            errors.append(f"{path}.helper '{helper}' is not an occurrence")
        edge = (parent, helper)
        if edge in seen_helpers:
            errors.append(f"{path} duplicates the helper link {parent} -> {helper}")
        seen_helpers.add(edge)
        if parent == helper:
            errors.append(f"{path} cannot attach '{helper}' to itself")

    return facts, tuple(errors)


def _check_alias(alias: str, path: str, errors: list[str]) -> None:
    if not _ALIAS_RE.fullmatch(alias):
        errors.append(
            f"{path} must start with a letter and contain at most 64 letters, numbers, underscores, or hyphens"
        )
    if alias.lower() in _RESERVED_ALIASES:
        errors.append(f"{path} cannot use the reserved alias '{alias}'")


def _build_graph(
    spec: WorkflowSpec,
    *,
    existing_ids: frozenset[str],
    new_ids: dict[str, str],
    base: WorkflowGraph | None = None,
) -> tuple[
    WorkflowGraph | None,
    dict[str, str],
    dict[str, str],
    tuple[str, ...],
]:
    graph = base or new_graph(
        AgentScope.PERSONAL,
        spec.name.strip(),
        description=spec.description.strip(),
    )
    task = graph.task_node
    result = graph.result_node
    if task is None or result is None:
        return None, {}, {}, ("could not create the Task and Aura Result nodes",)

    aliases_to_nodes = {"task": task.node_id, "result": result.node_id}
    node_aliases = {task.node_id: "task", result.node_id: "result"}
    nodes = [task, result]
    for item in spec.occurrences:
        agent_ref = item.agent_ref.strip()
        agent_id = new_ids.get(agent_ref, agent_ref if agent_ref in existing_ids else "")
        old = base.node(item.alias) if base is not None else None
        node = WorkflowNode(
            node_id=old.node_id if old is not None and old.is_agent else new_node_id(),
            kind=WorkflowNodeKind.AGENT,
            agent_id=agent_id,
            assignment=item.assignment.strip(),
        )
        if node.node_id in node_aliases:
            return None, {}, {}, ("could not mint a unique Agent occurrence id",)
        alias = item.alias.strip()
        aliases_to_nodes[alias] = node.node_id
        node_aliases[node.node_id] = alias
        nodes.append(node)

    connections: list[WorkflowConnection] = []
    connection_paths: dict[str, str] = {}
    for index, item in enumerate(spec.handoffs):
        connection = WorkflowConnection(
            connection_id=new_connection_id(),
            kind=ConnectionKind.STEP,
            source_id=aliases_to_nodes[item.source.strip()],
            target_id=aliases_to_nodes[item.target.strip()],
            order=index,
        )
        if connection.connection_id in connection_paths:
            return None, {}, {}, ("could not mint a unique handoff id",)
        if base is not None:
            connection = next(
                (
                    old
                    for old in base.connections
                    if (old.kind, old.source_id, old.target_id)
                    == (connection.kind, connection.source_id, connection.target_id)
                ),
                connection,
            )
        connections.append(connection)
        connection_paths[connection.connection_id] = f"handoffs[{index}]"

    helper_order = len(connections)
    for index, item in enumerate(spec.helpers):
        connection = WorkflowConnection(
            connection_id=new_connection_id(),
            kind=ConnectionKind.SUB_AGENT,
            source_id=aliases_to_nodes[item.parent.strip()],
            target_id=aliases_to_nodes[item.helper.strip()],
            order=helper_order + index,
        )
        if connection.connection_id in connection_paths:
            return None, {}, {}, ("could not mint a unique helper connection id",)
        if base is not None:
            connection = next(
                (
                    old
                    for old in base.connections
                    if (old.kind, old.source_id, old.target_id)
                    == (connection.kind, connection.source_id, connection.target_id)
                ),
                connection,
            )
        connections.append(connection)
        connection_paths[connection.connection_id] = f"helpers[{index}]"

    return (
        replace(
            graph,
            name=spec.name.strip(),
            description=spec.description.strip(),
            nodes=tuple(nodes),
            connections=tuple(connections),
        ),
        node_aliases,
        connection_paths,
        (),
    )


def _graph_issue_message(
    issue: GraphIssue,
    node_aliases: dict[str, str],
    connection_paths: dict[str, str],
) -> str:
    if issue.node_id:
        alias = node_aliases.get(issue.node_id, issue.node_id)
        return f"occurrence '{alias}': {issue.message}"
    if issue.connection_id:
        path = connection_paths.get(issue.connection_id, issue.connection_id)
        return f"{path}: {issue.message}"
    return issue.message
