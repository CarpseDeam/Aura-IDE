"""Model-facing schema for Aura's automatic, in-memory Agent teams.

The root describes specialists and their relationships with short aliases.
Aliases are deliberately not workflow ids, node ids, connection ids, or
canvas coordinates: the Agent team compiler owns translating this semantic
description into Aura's native workflow objects.

The schema stays flat so every supported provider, including small local
OpenAI-compatible models, sees the same straightforward contract. Solid
handoffs and optional helper relationships are separate arrays; nested helper
trees are expressed by more rows rather than a recursive JSON schema.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aura.agents.team_spec import (
    INHERIT_MODEL_TARGET_KEY,
    MAX_AGENT_TEAM_OCCURRENCES,
)

MAX_AGENT_TEAM_TASK_CHARS = 4000
MAX_AGENT_TEAM_INSTRUCTIONS_CHARS = 6000
MAX_AGENT_TEAM_ASSIGNMENT_CHARS = 4000

_ALIAS_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
_THINKING_VALUES = ("inherit", "off", "high", "max")
_PERMISSION_VALUES = ("read_only", "read_write")


def _one_line(value: object) -> str:
    """Return compact display text without letting one row dominate the tool."""
    return " ".join(str(value or "").split())


def _existing_agent_lines(rows: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        agent_id = _one_line(row.get("agent_id"))
        if not agent_id:
            continue
        name = _one_line(row.get("name")) or agent_id
        permission = _one_line(row.get("permission_label")) or "Read only"
        description = _one_line(row.get("description"))
        head = f"- {agent_id} — {name} [{permission}]"
        lines.append(f"{head}: {description}" if description else head)
    return "\n".join(lines)


def _model_target_rows(
    rows_or_keys: Iterable[Mapping[str, Any] | str],
) -> tuple[tuple[str, str], ...]:
    """Normalize frozen model-target rows or bare keys, preserving their order."""
    normalized: list[tuple[str, str]] = []
    seen = {INHERIT_MODEL_TARGET_KEY}
    for item in rows_or_keys:
        if isinstance(item, str):
            key = item.strip()
            label = key
        elif isinstance(item, Mapping):
            key = _one_line(
                item.get("key")
                or item.get("target_key")
                or item.get("model_target")
            )
            label = _one_line(item.get("label") or item.get("name")) or key
        else:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append((key, label))
    return tuple(normalized)


def _strict_object(
    properties: dict[str, Any], required: Iterable[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _alias(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": _ALIAS_PATTERN,
        "description": description,
    }


def build_run_agent_team_tool_def(
    existing_agents: Iterable[Mapping[str, Any]] | None = None,
    model_targets: Iterable[Mapping[str, Any] | str] | None = None,
) -> dict[str, Any]:
    """Build the root-only ``run_agent_team`` schema from frozen turn facts.

    ``existing_agents`` contains only the compact roster rows already safe for
    Aura's root catalog. ``model_targets`` accepts the frozen catalog's rows or
    its bare keys. Neither input is consulted again after this function returns.
    """
    agent_lines = _existing_agent_lines(tuple(existing_agents or ()))
    target_rows = _model_target_rows(tuple(model_targets or ()))
    target_keys = [INHERIT_MODEL_TARGET_KEY, *(key for key, _label in target_rows)]
    target_lines = "\n".join(
        f"- {key}" if label == key else f"- {key} — {label}"
        for key, label in target_rows
    )

    existing_copy = (
        "Existing Agents that may be reused by exact id:\n" + agent_lines
        if agent_lines
        else (
            "No existing Agents are available on this turn. Every agent_ref "
            "must therefore name an alias from new_agents."
        )
    )
    target_copy = (
        "Frozen model targets in addition to inherit:\n" + target_lines
        if target_lines
        else "No explicit model target is available; use inherit."
    )

    new_agent = _strict_object(
        {
            "alias": _alias(
                "A short name used only inside this team description."
            ),
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "The specialist's clear display name.",
            },
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "One short line describing this specialist's job.",
            },
            "instructions": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_TEAM_INSTRUCTIONS_CHARS,
                "description": (
                    "Stable instructions for how this specialist should approach "
                    "its job. Put task-specific work in the occurrence assignment."
                ),
            },
            "model_target": {
                "type": "string",
                "enum": target_keys,
                "description": (
                    "A frozen model-target key listed in this tool. inherit uses "
                    "Aura's current provider and model."
                ),
            },
            "thinking": {
                "type": "string",
                "enum": list(_THINKING_VALUES),
                "description": "This specialist's thinking level.",
            },
            "permission": {
                "type": "string",
                "enum": list(_PERMISSION_VALUES),
                "description": (
                    "read_only can investigate. read_write can propose changes "
                    "inside Aura's isolated Agent worktree."
                ),
            },
        },
        (
            "alias",
            "name",
            "description",
            "instructions",
            "model_target",
            "thinking",
            "permission",
        ),
    )
    occurrence = _strict_object(
        {
            "alias": _alias(
                "This placement's unique name, used by handoffs and helpers."
            ),
            "agent_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": (
                    "An alias from new_agents or an exact available existing-Agent "
                    "id. The same Agent may appear in several occurrences."
                ),
            },
            "assignment": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_TEAM_ASSIGNMENT_CHARS,
                "description": (
                    "What this specialist should do in this particular placement."
                ),
            },
        },
        ("alias", "agent_ref", "assignment"),
    )
    handoff = _strict_object(
        {
            "source": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "task or an occurrence alias.",
            },
            "target": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "result or an occurrence alias.",
            },
        },
        ("source", "target"),
    )
    helper = _strict_object(
        {
            "parent": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "The occurrence allowed to ask for help.",
            },
            "helper": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": (
                    "The optional helper occurrence. A helper may itself be a "
                    "parent in another row."
                ),
            },
        },
        ("parent", "helper"),
    )

    parameters = _strict_object(
        {
            "task": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_AGENT_TEAM_TASK_CHARS,
                "description": (
                    "The complete task the team should solve. Specialists cannot "
                    "see Aura's conversation, so include the necessary goal and "
                    "expected outcome."
                ),
            },
            "team_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "A short name for this temporary team.",
            },
            "team_description": {
                "type": "string",
                "maxLength": 500,
                "description": (
                    "A short explanation of why these specialists fit this task; "
                    "use an empty string when no explanation is needed."
                ),
            },
            "new_agents": {
                "type": "array",
                "maxItems": MAX_AGENT_TEAM_OCCURRENCES,
                "description": (
                    "New in-memory specialists needed for this run. Use an empty "
                    "array when every occurrence reuses an existing Agent."
                ),
                "items": new_agent,
            },
            "occurrences": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AGENT_TEAM_OCCURRENCES,
                "description": (
                    "Every specialist placement in this team, including optional "
                    "helpers. The total must be between one and six."
                ),
                "items": occurrence,
            },
            "handoffs": {
                "type": "array",
                "minItems": 2,
                "description": (
                    "Work that happens next. Use task and result as the two ends. "
                    "Several rows leaving one occurrence create branches; several "
                    "rows entering one occurrence make it wait for every branch."
                ),
                "items": handoff,
            },
            "helpers": {
                "type": "array",
                "maxItems": MAX_AGENT_TEAM_OCCURRENCES - 1,
                "description": (
                    "Optional help relationships. Keep this empty when none are "
                    "needed. Chaining flat rows creates nested helpers."
                ),
                "items": helper,
            },
        },
        (
            "task",
            "team_name",
            "team_description",
            "new_agents",
            "occurrences",
            "handoffs",
            "helpers",
        ),
    )

    return {
        "type": "function",
        "function": {
            "name": "run_agent_team",
            "description": (
                "Use Aura directly for ordinary work. Use exactly one occurrence "
                "when one specialist materially improves the task. Assemble two "
                "or more occurrences only when division of responsibility or "
                "structured handoffs materially improve the result. Run the "
                "chosen Agent or temporary team and wait for one Aura Result. A "
                "handoff sends work to the next specialist. Several outgoing "
                "handoffs branch; several incoming handoffs wait and join. Reuse "
                "one Agent in multiple occurrences when it has several assignments. "
                "A helper is optional and may have helpers of its own. Read / Write "
                "specialists use Aura's existing isolation and ordering rules. "
                f"Describe at most {MAX_AGENT_TEAM_OCCURRENCES} total occurrences; "
                "Aura preserves the team exactly and refuses an invalid shape "
                "rather than simplifying it.\n\n"
                + existing_copy
                + "\n\n"
                + target_copy
            ),
            "parameters": parameters,
        },
    }


__all__ = [
    "MAX_AGENT_TEAM_ASSIGNMENT_CHARS",
    "MAX_AGENT_TEAM_INSTRUCTIONS_CHARS",
    "MAX_AGENT_TEAM_OCCURRENCES",
    "MAX_AGENT_TEAM_TASK_CHARS",
    "build_run_agent_team_tool_def",
]
