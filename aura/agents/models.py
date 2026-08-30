"""The shape of one agent definition.

A definition is what an agent *is*: who it is, what it is for, what it is
told, and which model answers for it. It is deliberately not what an agent
is *allowed to do* — permission is private local state
(:mod:`aura.agents.local_state`), so a definition committed to a repository
can never hand itself authority on someone else's machine.

Both model-target fields move together: an agent either inherits the
provider and model Aura is currently using, or names both explicitly. Half a
target is not a fallback, it is a mistake, and the reader reports it as one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from aura.agents.identity import AgentScope


class AgentThinking(str, Enum):
    """The reasoning effort an agent runs with.

    ``INHERIT`` means "whatever Aura is set to", and is the default. The
    other three are the same modes the user can pick for Aura itself
    (:data:`aura.providers.base.THINKING_MODES`), named identically so the
    value can be handed straight to a provider when delegation lands.
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


@dataclass(frozen=True)
class ModelTarget:
    """Which provider and model answer for an agent.

    Empty strings on both fields mean "inherit". Exactly one filled field is
    invalid — see the module docstring.
    """

    provider: str = ""
    model: str = ""

    @property
    def inherits(self) -> bool:
        return not self.provider and not self.model

    @property
    def is_complete(self) -> bool:
        """True when this target is usable: either fully inherited or fully named."""
        return self.inherits or bool(self.provider and self.model)

    @classmethod
    def inherited(cls) -> "ModelTarget":
        return cls()

    @classmethod
    def explicit(cls, provider: str, model: str) -> "ModelTarget":
        return cls(provider=str(provider or "").strip(), model=str(model or "").strip())


@dataclass(frozen=True)
class AgentDefinition:
    """One agent, exactly as its Markdown file describes it.

    ``instructions`` is the Markdown body — the full brief the agent runs
    with. ``description`` is the short line Aura reads when deciding whether
    this agent is the right one to hand a piece of work to.
    """

    agent_id: str
    scope: AgentScope
    name: str
    description: str
    instructions: str
    target: ModelTarget = field(default_factory=ModelTarget)
    thinking: AgentThinking = AgentThinking.INHERIT

    @property
    def target_label(self) -> str:
        """How the model target reads in a list or a detail line."""
        if self.target.inherits:
            return "Inherits Aura's provider and model"
        return f"{self.target.provider} · {self.target.model}"


__all__ = [
    "THINKING_ORDER",
    "AgentDefinition",
    "AgentScope",
    "AgentThinking",
    "ModelTarget",
]
