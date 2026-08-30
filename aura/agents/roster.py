"""The frozen roster of agents one turn may delegate to.

A roster is composed once, before a turn's first provider request, and then
never moves.  It holds three facts per agent, resolved together so they can
never disagree with each other later in the turn:

* the **immutable definition** that was on disk when the turn began,
* the **effective local permission grant** this user gave it here, and
* the **order** the user put the agents in.

Editing a definition, reordering the roster, or changing a grant while a turn
is running therefore takes effect on the *next* turn — exactly like the frozen
skill turn state and the frozen tool catalog.

The roster also owns the only projection the root model is ever shown: id,
display name, and short description (:meth:`AgentTurnRoster.catalog_rows`).
An agent's full instructions are in the definition and stay there — they are
the *child's* brief, and they never enter the root's prompt or tool catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from aura.agents.local_state import DEFAULT_PERMISSION, AgentPermission
from aura.agents.models import AgentDefinition


@dataclass(frozen=True)
class AgentRosterEntry:
    """One eligible agent: what it is, and what it may do here."""

    definition: AgentDefinition
    permission: AgentPermission = DEFAULT_PERMISSION

    @property
    def agent_id(self) -> str:
        return self.definition.agent_id

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def description(self) -> str:
        return self.definition.description

    def catalog_row(self) -> dict[str, str]:
        """The compact identity and frozen effective grant shown to the root."""
        return {
            "agent_id": self.definition.agent_id,
            "name": self.definition.name,
            "description": self.definition.description,
            "permission": self.permission.value,
            "permission_label": self.permission.label,
        }


@dataclass(frozen=True)
class AgentTurnRoster:
    """The ordered, resolved agents one turn may delegate to.

    An empty roster is the ordinary case and means exactly one thing: this
    turn behaves like single-agent Aura always has — no delegation or retained
    change-set tool weight and no prompt block.
    """

    entries: tuple[AgentRosterEntry, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(entry.agent_id for entry in self.entries)

    def get(self, agent_id: str) -> AgentRosterEntry | None:
        """The entry for *agent_id*, or None when it is not on this roster.

        Membership is the whole authority: an id the model invents, or one
        the user removed after the turn began, resolves to None here and the
        delegation is refused.
        """
        for entry in self.entries:
            if entry.agent_id == agent_id:
                return entry
        return None

    def catalog_rows(self) -> tuple[dict[str, str], ...]:
        """Compact id/name/description rows, in the user's own order."""
        return tuple(entry.catalog_row() for entry in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


#: An explicit name for "this turn has no agents", so callers read as intent.
EMPTY_AGENT_ROSTER = AgentTurnRoster()


def resolve_agent_turn_roster(
    agent_ids: Iterable[str],
    *,
    definitions: Any,
    permissions: Any,
) -> AgentTurnRoster:
    """Freeze *agent_ids* into a roster of resolved definitions and grants.

    ``definitions`` is anything with ``get(agent_id) -> AgentDefinition | None``
    (an :class:`~aura.agents.store.AgentStore`), and ``permissions`` anything
    with ``permission(agent_id) -> AgentPermission`` (an
    :class:`~aura.agents.local_state.AgentLocalState`).  They are injected
    rather than constructed here so this function stays free of the filesystem
    and of the user's data directory.

    Order is the caller's, duplicates are dropped, and an id that no longer
    resolves to a valid definition is simply left out: an agent that cannot be
    read is not an agent Aura may hand work to.  A grant that cannot be read
    falls back to the least authority, never to more.
    """
    entries: list[AgentRosterEntry] = []
    seen: set[str] = set()
    for raw_id in agent_ids or ():
        agent_id = str(raw_id or "").strip()
        if not agent_id or agent_id in seen:
            continue
        seen.add(agent_id)
        try:
            definition = definitions.get(agent_id)
        except Exception:
            definition = None
        if definition is None:
            continue
        try:
            permission = AgentPermission(permissions.permission(agent_id))
        except Exception:
            permission = DEFAULT_PERMISSION
        entries.append(AgentRosterEntry(definition=definition, permission=permission))
    return AgentTurnRoster(entries=tuple(entries))


__all__ = [
    "EMPTY_AGENT_ROSTER",
    "AgentRosterEntry",
    "AgentTurnRoster",
    "resolve_agent_turn_roster",
]
