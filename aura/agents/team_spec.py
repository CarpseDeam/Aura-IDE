"""The semantic shape Aura uses to describe one temporary Agent team.

This is deliberately not a workflow document.  The model names reusable
specialists and their occurrences with short aliases, then describes solid
handoffs and dashed helper relationships between those aliases.  Opaque
Agent, node, connection, and workflow ids — along with canvas coordinates —
belong to the compiler in :mod:`aura.agents.team_compiler`.

The shape stays flat on purpose.  Nested helpers are represented by repeated
``parent``/``helper`` rows rather than a recursive JSON object, which keeps the
eventual tool contract friendly to OpenAI-compatible local models while still
expressing Aura's complete native topology.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

INHERIT_MODEL_TARGET_KEY = "inherit"
MAX_AGENT_TEAM_OCCURRENCES = 6


@dataclass(frozen=True)
class NewAgentSpec:
    """One reusable specialist Aura should create for this temporary team."""

    alias: str
    name: str
    description: str
    instructions: str
    model_target: str = INHERIT_MODEL_TARGET_KEY
    thinking: str = "inherit"
    permission: str = "read_only"


@dataclass(frozen=True)
class OccurrenceSpec:
    """One placement of a specialist, with this workflow's assignment."""

    alias: str
    agent_ref: str
    assignment: str


@dataclass(frozen=True)
class HandoffSpec:
    """One solid next-step relationship.

    ``source`` may be ``task`` or an occurrence alias.  ``target`` may be an
    occurrence alias or ``result``.
    """

    source: str
    target: str


@dataclass(frozen=True)
class HelperSpec:
    """One dashed optional-helper relationship between two occurrences."""

    parent: str
    helper: str


@dataclass(frozen=True)
class WorkflowSpec:
    """Reusable work and relationships, independent of any execution task."""

    name: str
    description: str = ""
    new_agents: tuple[NewAgentSpec, ...] = ()
    occurrences: tuple[OccurrenceSpec, ...] = ()
    handoffs: tuple[HandoffSpec, ...] = ()
    helpers: tuple[HelperSpec, ...] = ()


@dataclass(frozen=True)
class AgentTeamSpec:
    """A task and the semantic team Aura assembled to perform it."""

    task: str
    name: str
    description: str = ""
    new_agents: tuple[NewAgentSpec, ...] = ()
    occurrences: tuple[OccurrenceSpec, ...] = ()
    handoffs: tuple[HandoffSpec, ...] = ()
    helpers: tuple[HelperSpec, ...] = ()

    @property
    def workflow(self) -> WorkflowSpec:
        return WorkflowSpec(
            self.name, self.description, self.new_agents,
            self.occurrences, self.handoffs, self.helpers,
        )


@dataclass(frozen=True)
class ParsedWorkflowSpec:
    spec: WorkflowSpec | None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.errors


@dataclass(frozen=True)
class ParsedAgentTeamSpec:
    """One model/tool payload parsed into semantic rows, or its errors."""

    spec: AgentTeamSpec | None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.errors


def parse_agent_team_spec(raw: Mapping[str, Any] | object) -> ParsedAgentTeamSpec:
    """Parse the run task separately from its reusable Workflow shape."""
    if not isinstance(raw, Mapping):
        return ParsedAgentTeamSpec(None, ("the Agent team payload is not an object",))
    errors: list[str] = []
    task = _text(raw, "task", "task", errors, required=True)
    parsed = parse_workflow_spec(raw, name_key="team_name", description_key="team_description")
    errors.extend(parsed.errors)
    if errors or parsed.spec is None:
        return ParsedAgentTeamSpec(None, tuple(errors))
    spec = parsed.spec
    return ParsedAgentTeamSpec(AgentTeamSpec(
        task, spec.name, spec.description, spec.new_agents,
        spec.occurrences, spec.handoffs, spec.helpers,
    ))


def parse_workflow_spec(
    raw: Mapping[str, Any] | object, *, name_key: str = "name", description_key: str = "description"
) -> ParsedWorkflowSpec:
    """Parse the flat semantic shape without inventing missing values.

    Value-level rules — aliases, references, topology, limits, model targets,
    and permissions — are owned by the graph builder. This boundary only proves
    the payload is an object containing text fields and arrays of objects.
    Automatic teams supply their own top-level name and description keys.
    """
    if not isinstance(raw, Mapping):
        return ParsedWorkflowSpec(None, ("the Workflow payload is not an object",))

    errors: list[str] = []
    name = _text(raw, name_key, name_key, errors, required=True)
    description = _text(raw, description_key, description_key, errors)

    new_agents = tuple(
        NewAgentSpec(
            alias=_text(row, "alias", f"new_agents[{index}].alias", errors, required=True),
            name=_text(row, "name", f"new_agents[{index}].name", errors, required=True),
            description=_text(
                row,
                "description",
                f"new_agents[{index}].description",
                errors,
                required=True,
            ),
            instructions=_text(
                row,
                "instructions",
                f"new_agents[{index}].instructions",
                errors,
                required=True,
            ),
            model_target=_text(
                row,
                "model_target",
                f"new_agents[{index}].model_target",
                errors,
                default=INHERIT_MODEL_TARGET_KEY,
            ),
            thinking=_text(
                row,
                "thinking",
                f"new_agents[{index}].thinking",
                errors,
                default="inherit",
            ),
            permission=_text(
                row,
                "permission",
                f"new_agents[{index}].permission",
                errors,
                default="read_only",
            ),
        )
        for index, row in enumerate(_object_rows(raw, "new_agents", errors))
    )
    occurrences = tuple(
        OccurrenceSpec(
            alias=_text(row, "alias", f"occurrences[{index}].alias", errors, required=True),
            agent_ref=_text(
                row,
                "agent_ref",
                f"occurrences[{index}].agent_ref",
                errors,
                required=True,
            ),
            assignment=_text(
                row,
                "assignment",
                f"occurrences[{index}].assignment",
                errors,
                required=True,
            ),
        )
        for index, row in enumerate(_object_rows(raw, "occurrences", errors))
    )
    handoffs = tuple(
        HandoffSpec(
            source=_text(row, "source", f"handoffs[{index}].source", errors, required=True),
            target=_text(row, "target", f"handoffs[{index}].target", errors, required=True),
        )
        for index, row in enumerate(_object_rows(raw, "handoffs", errors))
    )
    helpers = tuple(
        HelperSpec(
            parent=_text(row, "parent", f"helpers[{index}].parent", errors, required=True),
            helper=_text(row, "helper", f"helpers[{index}].helper", errors, required=True),
        )
        for index, row in enumerate(_object_rows(raw, "helpers", errors))
    )

    if errors:
        return ParsedWorkflowSpec(None, tuple(errors))
    return ParsedWorkflowSpec(
        WorkflowSpec(
            name=name,
            description=description,
            new_agents=new_agents,
            occurrences=occurrences,
            handoffs=handoffs,
            helpers=helpers,
        )
    )


def _object_rows(
    document: Mapping[str, Any], key: str, errors: list[str]
) -> tuple[Mapping[str, Any], ...]:
    raw = document.get(key, ())
    if not isinstance(raw, (list, tuple)):
        errors.append(f"{key} must be an array")
        return ()
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            errors.append(f"{key}[{index}] is not an object")
            continue
        rows.append(item)
    return tuple(rows)


def _text(
    document: Mapping[str, Any],
    key: str,
    path: str,
    errors: list[str],
    *,
    required: bool = False,
    default: str = "",
) -> str:
    if key not in document:
        if required:
            errors.append(f"{path} is required")
        return default
    raw = document.get(key)
    if not isinstance(raw, str):
        errors.append(f"{path} must be text")
        return default
    return raw.strip()


__all__ = [
    "INHERIT_MODEL_TARGET_KEY",
    "MAX_AGENT_TEAM_OCCURRENCES",
    "AgentTeamSpec",
    "HandoffSpec",
    "HelperSpec",
    "NewAgentSpec",
    "OccurrenceSpec",
    "ParsedAgentTeamSpec",
    "ParsedWorkflowSpec",
    "WorkflowSpec",
    "parse_agent_team_spec",
    "parse_workflow_spec",
]
