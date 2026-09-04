"""The complete Agent capability frozen for one root Aura turn.

Agent availability is a three-way choice, not a collection of independent
flags.  A turn either has no Agent path, may assemble one temporary team from
its frozen roster and model targets, or may run one already-frozen workflow.
Keeping that choice in one value prevents a queued turn from accidentally
mixing authority captured at different moments.

This module contains only immutable data.  It does not read local state,
discover models, build tool schemas, run workflows, or decide how often an
automatic team may be launched.  Those owners construct and consume this
snapshot at the submission and runtime boundaries respectively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from aura.agents.models import CURRENT_MODEL_LABEL
from aura.agents.roster import EMPTY_AGENT_ROSTER, AgentTurnRoster
from aura.agents.team_spec import INHERIT_MODEL_TARGET_KEY
from aura.agents.workflow_plan import WorkflowRunPlan


class AgentTurnMode(str, Enum):
    """The one Agent path available to a submitted root turn."""

    OFF = "off"
    AUTOMATIC = "automatic"
    ACTIVE_WORKFLOW = "active_workflow"


@dataclass(frozen=True, slots=True)
class AgentModelTarget:
    """One provider-qualified model choice addressable by a stable key.

    The special ``inherit`` row has empty provider/model values and means the
    generated Agent follows the submitted root turn.  Every other row is an
    exact provider/model pair captured when the turn was submitted; no
    endpoint or credential is retained here.
    """

    key: str
    provider: str
    model: str
    label: str = ""

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        provider = str(self.provider or "").strip()
        model = str(self.model or "").strip()
        label = str(self.label or "").strip()

        if not key:
            raise ValueError("An Agent model target needs a key.")
        if "\n" in key or "\r" in key:
            raise ValueError("An Agent model target key must be a single line.")
        if key == INHERIT_MODEL_TARGET_KEY:
            if provider or model:
                raise ValueError("The inherit Agent model target may not name a provider or model.")
            label = label or CURRENT_MODEL_LABEL
        elif not provider or not model:
            raise ValueError("An explicit Agent model target needs both a provider and model.")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "label", label or f"{provider} · {model}")

    @property
    def inherits(self) -> bool:
        return self.key == INHERIT_MODEL_TARGET_KEY

    def catalog_row(self) -> dict[str, str]:
        """A fresh model-facing projection that cannot mutate this snapshot."""
        return {
            "key": self.key,
            "provider": self.provider,
            "model": self.model,
            "label": self.label,
        }


INHERIT_MODEL_TARGET = AgentModelTarget(
    key=INHERIT_MODEL_TARGET_KEY,
    provider="",
    model="",
    label=CURRENT_MODEL_LABEL,
)


@dataclass(frozen=True, slots=True)
class AgentModelTargets:
    """An ordered, immutable model-target catalog for one submitted turn.

    ``inherit`` is always present and always first.  Caller order for every
    explicit target is otherwise preserved because the same order is shown in
    the tool schema.  Duplicate keys are refused rather than silently choosing
    which provider/model pair a generated Agent would receive.
    """

    targets: tuple[AgentModelTarget, ...] = ()
    _by_key: Mapping[str, AgentModelTarget] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        supplied = tuple(self.targets or ())
        if any(not isinstance(target, AgentModelTarget) for target in supplied):
            raise TypeError("Agent model targets must be AgentModelTarget values.")

        by_key: dict[str, AgentModelTarget] = {}
        for target in supplied:
            if target.key in by_key:
                raise ValueError(f"More than one Agent model target uses the key '{target.key}'.")
            by_key[target.key] = target

        inherited = by_key.get(INHERIT_MODEL_TARGET_KEY, INHERIT_MODEL_TARGET)
        ordered = (
            inherited,
            *(target for target in supplied if not target.inherits),
        )
        object.__setattr__(self, "targets", ordered)
        object.__setattr__(
            self,
            "_by_key",
            MappingProxyType({target.key: target for target in ordered}),
        )

    @classmethod
    def freeze(cls, targets: Iterable[AgentModelTarget] = ()) -> "AgentModelTargets":
        """Snapshot an arbitrary iterable without retaining its container."""
        return cls(tuple(targets))

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(target.key for target in self.targets)

    @property
    def mapping(self) -> Mapping[str, AgentModelTarget]:
        """The frozen key lookup, for non-model-facing consumers."""
        return self._by_key

    def get(self, key: str) -> AgentModelTarget | None:
        return self._by_key.get(str(key or "").strip())

    def catalog_rows(self) -> tuple[dict[str, str], ...]:
        return tuple(target.catalog_row() for target in self.targets)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._by_key

    def __iter__(self) -> Iterator[AgentModelTarget]:
        return iter(self.targets)

    def __len__(self) -> int:
        return len(self.targets)


DEFAULT_AGENT_MODEL_TARGETS = AgentModelTargets()


@dataclass(frozen=True, slots=True)
class AgentTurnContext:
    """One internally consistent Agent capability captured for a root turn."""

    mode: AgentTurnMode = AgentTurnMode.OFF
    roster: AgentTurnRoster = EMPTY_AGENT_ROSTER
    workflow_plan: WorkflowRunPlan | None = None
    model_targets: AgentModelTargets = DEFAULT_AGENT_MODEL_TARGETS
    root_provider: str = ""
    root_model: str = ""
    root_thinking: str = "off"

    def __post_init__(self) -> None:
        try:
            mode = AgentTurnMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unknown Agent turn mode: {self.mode!r}.") from exc
        object.__setattr__(self, "mode", mode)

        if not isinstance(self.roster, AgentTurnRoster):
            raise TypeError("An Agent turn context needs a frozen AgentTurnRoster.")
        if not isinstance(self.model_targets, AgentModelTargets):
            raise TypeError("An Agent turn context needs frozen AgentModelTargets.")
        if self.workflow_plan is not None and not isinstance(
            self.workflow_plan, WorkflowRunPlan
        ):
            raise TypeError("An Agent turn context needs a frozen WorkflowRunPlan.")
        root_provider = str(self.root_provider or "").strip()
        root_model = str(self.root_model or "").strip()
        root_thinking = str(self.root_thinking or "off").strip() or "off"
        object.__setattr__(self, "root_provider", root_provider)
        object.__setattr__(self, "root_model", root_model)
        object.__setattr__(self, "root_thinking", root_thinking)

        if mode is AgentTurnMode.OFF:
            if not self.roster.is_empty or self.workflow_plan is not None:
                raise ValueError("An off Agent turn cannot carry a roster or workflow plan.")
            if len(self.model_targets) != 1:
                raise ValueError("An off Agent turn cannot carry explicit model targets.")
            if root_provider or root_model or root_thinking != "off":
                raise ValueError("An off Agent turn cannot carry a root model target.")
            return

        if mode is AgentTurnMode.AUTOMATIC:
            if self.workflow_plan is not None:
                raise ValueError("An automatic Agent turn cannot carry an active workflow plan.")
            if bool(root_provider) != bool(root_model):
                raise ValueError(
                    "An automatic Agent turn's root provider and model must be "
                    "captured together."
                )
            return

        if self.workflow_plan is None:
            raise ValueError("An active-workflow Agent turn needs a frozen workflow plan.")
        if not self.roster.is_empty:
            raise ValueError("A saved-workflow Agent turn cannot also expose an automatic roster.")
        if len(self.model_targets) != 1:
            raise ValueError("A saved-workflow Agent turn cannot carry automatic model targets.")
        if root_provider or root_model or root_thinking != "off":
            raise ValueError("A saved-workflow Agent turn cannot carry an automatic root target.")

    @classmethod
    def off(cls) -> "AgentTurnContext":
        return cls()

    @classmethod
    def automatic(
        cls,
        *,
        roster: AgentTurnRoster = EMPTY_AGENT_ROSTER,
        model_targets: AgentModelTargets | Iterable[AgentModelTarget] = (),
        root_provider: str = "",
        root_model: str = "",
        root_thinking: str = "off",
    ) -> "AgentTurnContext":
        frozen_targets = (
            model_targets if isinstance(model_targets, AgentModelTargets) else AgentModelTargets.freeze(model_targets)
        )
        return cls(
            mode=AgentTurnMode.AUTOMATIC,
            roster=roster,
            model_targets=frozen_targets,
            root_provider=root_provider,
            root_model=root_model,
            root_thinking=root_thinking,
        )

    @classmethod
    def active_workflow(cls, plan: WorkflowRunPlan) -> "AgentTurnContext":
        return cls(
            mode=AgentTurnMode.ACTIVE_WORKFLOW,
            workflow_plan=plan,
        )

    @property
    def enabled(self) -> bool:
        return self.mode is not AgentTurnMode.OFF

    @property
    def is_automatic(self) -> bool:
        return self.mode is AgentTurnMode.AUTOMATIC

    @property
    def has_active_workflow(self) -> bool:
        return self.mode is AgentTurnMode.ACTIVE_WORKFLOW


EMPTY_AGENT_TURN_CONTEXT = AgentTurnContext.off()


__all__ = [
    "DEFAULT_AGENT_MODEL_TARGETS",
    "EMPTY_AGENT_TURN_CONTEXT",
    "INHERIT_MODEL_TARGET",
    "INHERIT_MODEL_TARGET_KEY",
    "AgentModelTarget",
    "AgentModelTargets",
    "AgentTurnContext",
    "AgentTurnMode",
]
