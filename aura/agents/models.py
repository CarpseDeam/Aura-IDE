"""The shape of one agent definition.

A definition is what an agent *is*: who it is, what it is for, what it is
told, and which model answers for it. It is deliberately not what an agent
is *allowed to do* — permission is private local state
(:mod:`aura.agents.local_state`), so a definition committed to a repository
can never hand itself authority on someone else's machine.

A definition may name a provider and model target without carrying any of the
machine-local configuration that makes that provider usable. Empty values
inherit Aura's submitted turn. A model on its own keeps the historical
behaviour of resolving under Aura's provider; a provider on its own follows
that provider's configured default model.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aura.agents.identity import AgentScope


class AgentThinking(str, Enum):
    """The reasoning effort an agent runs with.

    ``INHERIT`` means "whatever Aura is set to", and is the default. The
    other three are the same modes the user can pick for Aura itself
    (:data:`aura.providers.base.THINKING_MODES`), named identically so the
    value can be handed straight to a provider.
    """

    INHERIT = "inherit"
    OFF = "off"
    HIGH = "high"
    MAX = "max"

    @property
    def inherits(self) -> bool:
        return self is AgentThinking.INHERIT

    @property
    def label(self) -> str:
        return _THINKING_LABELS[self]

    @classmethod
    def parse(cls, raw: object) -> "AgentThinking | None":
        """Parse a stored value, or return None when it is not one of ours."""
        if isinstance(raw, AgentThinking):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


_THINKING_LABELS: dict[AgentThinking, str] = {
    AgentThinking.INHERIT: "Inherit",
    AgentThinking.OFF: "Off",
    AgentThinking.HIGH: "High",
    AgentThinking.MAX: "Max",
}

#: Display order for the thinking control.
THINKING_ORDER: tuple[AgentThinking, ...] = (
    AgentThinking.INHERIT,
    AgentThinking.OFF,
    AgentThinking.HIGH,
    AgentThinking.MAX,
)

#: How a definition that names no model of its own reads in a list.
CURRENT_MODEL_LABEL = "Aura's current model"


@dataclass(frozen=True)
class AgentDefinition:
    """One agent, exactly as its Markdown file describes it.

    ``instructions`` is the Markdown body — the full brief the agent runs
    with. ``description`` is the short line Aura reads when deciding whether
    this agent is the right one to hand a piece of work to. ``provider`` and
    ``model`` are the optional target identifiers; empty values follow the
    compatibility matrix described in the module docstring.
    """

    agent_id: str
    scope: AgentScope
    name: str
    description: str
    instructions: str
    provider: str = ""
    model: str = ""
    thinking: AgentThinking = AgentThinking.INHERIT

    @property
    def model_label(self) -> str:
        """How the qualified model choice reads in a list or detail line."""
        provider = self.provider.strip()
        model = self.model.strip()
        if provider:
            return f"{provider} · {model or 'default model'}"
        if model:
            return f"Aura's provider · {model}"
        return CURRENT_MODEL_LABEL


__all__ = [
    "CURRENT_MODEL_LABEL",
    "THINKING_ORDER",
    "AgentDefinition",
    "AgentScope",
    "AgentThinking",
]
