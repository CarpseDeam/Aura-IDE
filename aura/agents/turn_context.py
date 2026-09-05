"""The complete Agent capability frozen for one root Aura turn.

Agent availability is one honest gate. A turn is either off, or it receives
the frozen roster, model targets, and every saved Workflow which was runnable
at submission. Keeping those facts in one value prevents a queued turn from
accidentally mixing authority captured at different moments.

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
    """Whether the submitted root turn has Agent capability."""

    OFF = "off"
    ENABLED = "enabled"


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
class AgentWorkflowCatalog:
    """Every runnable saved Workflow frozen for one submitted turn."""

    plans: tuple[WorkflowRunPlan, ...] = ()
    _by_id: Mapping[str, WorkflowRunPlan] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        supplied = tuple(self.plans or ())
        if any(not isinstance(plan, WorkflowRunPlan) for plan in supplied):
            raise TypeError("An Agent workflow catalog needs WorkflowRunPlan values.")
        by_id: dict[str, WorkflowRunPlan] = {}
        for plan in supplied:
            if plan.graph_id in by_id:
                raise ValueError(
                    f"More than one frozen Workflow uses the id '{plan.graph_id}'."
                )
            by_id[plan.graph_id] = plan
        object.__setattr__(self, "plans", supplied)
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))

    @classmethod
    def freeze(
        cls, plans: Iterable[WorkflowRunPlan] = ()
    ) -> "AgentWorkflowCatalog":
        return cls(tuple(plans))

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(plan.graph_id for plan in self.plans)

    def get(self, workflow_id: str) -> WorkflowRunPlan | None:
        return self._by_id.get(str(workflow_id or "").strip())

    def catalog_rows(self) -> tuple[dict[str, str], ...]:
        """Only the concise saved-Workflow facts Aura needs to choose."""
        return tuple(
            {
                "workflow_id": plan.graph_id,
                "name": plan.name,
                "description": plan.description,
            }
            for plan in self.plans
        )

    def __iter__(self) -> Iterator[WorkflowRunPlan]:
        return iter(self.plans)

    def __len__(self) -> int:
        return len(self.plans)


EMPTY_AGENT_WORKFLOW_CATALOG = AgentWorkflowCatalog()


@dataclass(frozen=True, slots=True)
class AgentTurnContext:
    """One internally consistent Agent capability captured for a root turn."""

    mode: AgentTurnMode = AgentTurnMode.OFF
    roster: AgentTurnRoster = EMPTY_AGENT_ROSTER
    workflows: AgentWorkflowCatalog = EMPTY_AGENT_WORKFLOW_CATALOG
    model_targets: AgentModelTargets = DEFAULT_AGENT_MODEL_TARGETS
    root_provider: str = ""
    root_model: str = ""
    root_thinking: str = "off"
    explicit_workflow_id: str = ""

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
        if not isinstance(self.workflows, AgentWorkflowCatalog):
            raise TypeError("An Agent turn context needs a frozen AgentWorkflowCatalog.")
        root_provider = str(self.root_provider or "").strip()
        root_model = str(self.root_model or "").strip()
        root_thinking = str(self.root_thinking or "off").strip() or "off"
        object.__setattr__(self, "root_provider", root_provider)
        object.__setattr__(self, "root_model", root_model)
        object.__setattr__(self, "root_thinking", root_thinking)

        if self.explicit_workflow_id:
            if mode is AgentTurnMode.OFF or self.workflows.ids != (self.explicit_workflow_id,):
                raise ValueError("An explicit Run must carry exactly its requested Workflow.")

        if mode is AgentTurnMode.OFF:
            if not self.roster.is_empty or len(self.workflows):
                raise ValueError("An off Agent turn cannot carry Agents or Workflows.")
            if len(self.model_targets) != 1:
                raise ValueError("An off Agent turn cannot carry explicit model targets.")
            if root_provider or root_model or root_thinking != "off":
                raise ValueError("An off Agent turn cannot carry a root model target.")
            return

        if bool(root_provider) != bool(root_model):
            raise ValueError(
                "An enabled Agent turn's root provider and model must be captured together."
            )

    @classmethod
    def off(cls) -> "AgentTurnContext":
        return cls()

    @classmethod
    def enabled(
        cls,
        *,
        roster: AgentTurnRoster = EMPTY_AGENT_ROSTER,
        workflows: AgentWorkflowCatalog | Iterable[WorkflowRunPlan] = (),
        model_targets: AgentModelTargets | Iterable[AgentModelTarget] = (),
        root_provider: str = "",
        root_model: str = "",
        root_thinking: str = "off",
        explicit_workflow_id: str = "",
    ) -> "AgentTurnContext":
        frozen_targets = (
            model_targets if isinstance(model_targets, AgentModelTargets) else AgentModelTargets.freeze(model_targets)
        )
        frozen_workflows = (
            workflows
            if isinstance(workflows, AgentWorkflowCatalog)
            else AgentWorkflowCatalog.freeze(workflows)
        )
        return cls(
            mode=AgentTurnMode.ENABLED,
            roster=roster,
            workflows=frozen_workflows,
            model_targets=frozen_targets,
            root_provider=root_provider,
            root_model=root_model,
            root_thinking=root_thinking,
            explicit_workflow_id=explicit_workflow_id,
        )

    @property
    def is_enabled(self) -> bool:
        return self.mode is not AgentTurnMode.OFF

    def workflow(self, workflow_id: str) -> WorkflowRunPlan | None:
        """Resolve an id only against this submitted immutable catalog."""
        return self.workflows.get(workflow_id)


EMPTY_AGENT_TURN_CONTEXT = AgentTurnContext.off()


__all__ = [
    "DEFAULT_AGENT_MODEL_TARGETS",
    "EMPTY_AGENT_TURN_CONTEXT",
    "EMPTY_AGENT_WORKFLOW_CATALOG",
    "INHERIT_MODEL_TARGET",
    "INHERIT_MODEL_TARGET_KEY",
    "AgentModelTarget",
    "AgentModelTargets",
    "AgentTurnContext",
    "AgentTurnMode",
    "AgentWorkflowCatalog",
]
