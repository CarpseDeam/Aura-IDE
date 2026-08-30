"""Reading and writing one agent definition as human-readable Markdown.

The file format is a YAML front-matter block followed by the instructions,
written so a person can author or review one in a text editor and a diff of
one reads as prose:

    ---
    id: 6f1c...            # minted once, never edited
    name: Reviewer
    description: Reviews a diff for correctness bugs.
    model: claude-sonnet-4-6   # omitted entirely for Aura's current model
    thinking: high
    ---

    Read the diff and report only defects you can demonstrate.

Three rules keep the format honest. The declared ``id`` must equal the file
name stem, so identity is visible in the directory listing and can never be
ambiguous. A definition may not declare permission of any kind: authority is
private local state, so a definition committed to a repository cannot grant
itself anything on a machine that merely opened the project. And a definition
may not choose a provider — an agent runs on whichever provider Aura is set
to. A ``provider:`` key left over from an older definition is read as the
noise it now is: dropped on load, never honoured, and gone the next time the
file is written. It is normalization in one direction only, so there is no
second provider path to keep in step with the first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from aura.agents.identity import AgentScope, is_valid_agent_id
from aura.agents.models import AgentDefinition, AgentThinking
from aura.agents.validation import agent_name_error, delegation_description_error

_DELIMITER = "---"

#: Front-matter keys a definition may never declare. A project definition
#: that names one is refused outright rather than loaded with the key
#: ignored, so a repository cannot ship a file that looks like it grants
#: authority and quietly does nothing.
RESERVED_KEYS: tuple[str, ...] = (
    "permission",
    "permissions",
    "grant",
    "grants",
    "authority",
    "allow",
    "allowed",
    "worktree",
    "terminal",
)


@dataclass(frozen=True)
class ParsedAgent:
    """One parse attempt: a definition, or the reasons there isn't one."""

    definition: AgentDefinition | None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.definition is not None and not self.errors


def parse_agent_document(
    raw: str,
    *,
    scope: AgentScope,
    expected_id: str,
) -> ParsedAgent:
    """Parse one definition file's text against the id its file name claims."""
    errors: list[str] = []
    text = str(raw or "").strip("﻿")
    lines = text.splitlines()
    if not text.strip():
        return ParsedAgent(None, ("the definition file is empty",))
    if not lines or lines[0].strip() != _DELIMITER:
        return ParsedAgent(None, ("the definition is missing its YAML front matter",))

    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _DELIMITER:
            end_index = index
            break
    if end_index is None:
        return ParsedAgent(None, ("front matter is missing its closing '---'",))

    block = "\n".join(lines[1:end_index])
    try:
        loaded = yaml.safe_load(block) if block.strip() else {}
    except yaml.YAMLError as exc:
        return ParsedAgent(None, (f"front matter is not valid YAML: {exc}",))
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return ParsedAgent(None, ("front matter must be a YAML mapping",))

    declared = {str(key).strip().lower() for key in loaded}
    for reserved in RESERVED_KEYS:
        if reserved in declared:
            errors.append(
                f"'{reserved}' is not allowed in a definition — permission is "
                "granted locally, per user, and never by the definition itself"
            )

    agent_id = _string(loaded.get("id"))
    if not agent_id:
        errors.append("'id' is required")
    elif not is_valid_agent_id(agent_id):
        errors.append(f"'{agent_id}' is not a valid agent id")
    elif agent_id != expected_id:
        errors.append(f"declared id '{agent_id}' does not match the file name '{expected_id}.md'")

    name = _string(loaded.get("name"))
    name_error = agent_name_error(name)
    if name_error:
        errors.append(name_error)

    description = _string(loaded.get("description"))
    description_error = delegation_description_error(description)
    if description_error:
        errors.append(description_error)

    model = _string(loaded.get("model"))

    thinking = AgentThinking.parse(loaded.get("thinking", AgentThinking.INHERIT.value))
    if thinking is None:
        errors.append("'thinking' must be one of: inherit, off, high, max")
        thinking = AgentThinking.INHERIT

    instructions = "\n".join(lines[end_index + 1:]).strip()
    if not instructions:
        errors.append("the definition has no instructions after its front matter")

    if errors:
        return ParsedAgent(None, tuple(errors))

    return ParsedAgent(
        AgentDefinition(
            agent_id=expected_id,
            scope=scope,
            name=name,
            description=description,
            instructions=instructions,
            model=model,
            thinking=thinking,
        )
    )


def render_agent_document(definition: AgentDefinition) -> str:
    """Serialize *definition* back to the Markdown a person would have written."""
    front: dict[str, Any] = {
        "id": definition.agent_id,
        "name": definition.name,
        "description": definition.description,
    }
    if definition.model.strip():
        front["model"] = definition.model.strip()
    front["thinking"] = definition.thinking.value

    block = yaml.safe_dump(
        front, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()
    body = definition.instructions.strip()
    return f"{_DELIMITER}\n{block}\n{_DELIMITER}\n\n{body}\n"


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "RESERVED_KEYS",
    "ParsedAgent",
    "parse_agent_document",
    "render_agent_document",
]
