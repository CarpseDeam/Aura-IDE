"""Turning one discovered agent plus this user's private state into a row.

Pure functions, no widgets and no storage. A definition says what an agent
*is*; availability and permission say what one person has decided about it
here. Both are needed to draw a row or fill the editor, and joining them is a
small rule that is easier to keep honest in one place than in a controller
method: a definition that did not load is never shown as available, whatever
the local roster still remembers about it, and an id that is not addressable
never carries a grant.
"""

from __future__ import annotations

from aura.agents.identity import is_valid_agent_id
from aura.agents.local_state import DEFAULT_PERMISSION, AgentPermission
from aura.agents.models import AgentThinking
from aura.agents.store import AgentSummary
from aura.gui.agents_editor import AgentDetail
from aura.gui.agents_library import AgentRow


def agent_row(
    summary: AgentSummary, *, available: bool, permission: AgentPermission
) -> AgentRow:
    """One library row, joined from a definition and a private decision."""
    definition = summary.definition
    addressable = is_valid_agent_id(summary.agent_id)
    return AgentRow(
        agent_id=summary.agent_id,
        scope=summary.scope.value,
        name=summary.name,
        description=summary.description,
        model_label=definition.model_label if definition else "",
        thinking_label=definition.thinking.label if definition else "",
        available=bool(summary.valid and addressable and available),
        permission=permission if addressable else DEFAULT_PERMISSION,
        valid=summary.valid,
        errors=summary.errors,
    )


def agent_detail(
    summary: AgentSummary, *, available: bool, permission: AgentPermission
) -> AgentDetail:
    """The reusable-settings form for one agent, ready to render."""
    definition = summary.definition
    addressable = is_valid_agent_id(summary.agent_id)
    return AgentDetail(
        agent_id=summary.agent_id,
        scope=summary.scope.value,
        name=summary.name,
        description=summary.description,
        instructions=definition.instructions if definition else "",
        model=definition.model if definition else "",
        thinking=definition.thinking if definition else AgentThinking.INHERIT,
        permission=permission if addressable else DEFAULT_PERMISSION,
        available=bool(addressable and available),
        valid=summary.valid,
        errors=summary.errors,
    )


__all__ = ["agent_detail", "agent_row"]
