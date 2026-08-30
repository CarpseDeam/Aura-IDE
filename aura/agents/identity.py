"""Stable agent identity and the two places an agent definition can live.

An agent is identified by an opaque, immutable id that is generated once, at
creation, and never derived from anything the user can edit. Display names
are labels: they are expected to change, they are expected to collide, and
nothing in the product resolves an agent by one.

Scope is not stored in a definition — it is read off the directory the file
was discovered in, so moving a definition between the project and the
personal library is the whole act of changing its scope.
"""
from __future__ import annotations

import re
import uuid
from enum import Enum

#: The scopes an agent definition can be discovered in, in display order.
SCOPE_ORDER: tuple[str, ...] = ("project", "personal")

#: An id is used verbatim as a file name stem, so it may not carry a path
#: separator, a drive letter, a leading dot, or any other character that
#: would let a definition name a location instead of itself.
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class AgentScope(str, Enum):
    """Where an agent definition's Markdown file lives.

    ``PROJECT`` definitions travel with the repository and are visible to
    everyone who opens it. ``PERSONAL`` definitions live in the user's own
    Aura data directory and are visible only to them. Neither carries any
    authority; that is local state, per user, and never part of the file.
    """

    PROJECT = "project"
    PERSONAL = "personal"

    @property
    def label(self) -> str:
        return "Project" if self is AgentScope.PROJECT else "Personal"


def new_agent_id() -> str:
    """Mint an id for a brand-new agent.

    Opaque on purpose: nothing about the agent's name, scope, or purpose is
    recoverable from it, so renaming an agent can never be mistaken for
    creating a different one.
    """
    return uuid.uuid4().hex


def is_valid_agent_id(raw: object) -> bool:
    """True for an id that is safe to use as a definition's file name."""
    return isinstance(raw, str) and bool(_AGENT_ID_RE.match(raw))


__all__ = [
    "SCOPE_ORDER",
    "AgentScope",
    "is_valid_agent_id",
    "new_agent_id",
]
