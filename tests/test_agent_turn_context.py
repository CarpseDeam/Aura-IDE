"""The frozen Agent turn context permits exactly one Agent path."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from aura.agents.graph_models import WorkflowGraph
from aura.agents.identity import AgentScope
from aura.agents.models import AgentDefinition
from aura.agents.roster import AgentRosterEntry, AgentTurnRoster
from aura.agents.turn_context import (
    INHERIT_MODEL_TARGET_KEY,
    AgentModelTarget,
    AgentModelTargets,
    AgentTurnContext,
    AgentTurnMode,
)
from aura.agents.workflow_plan import WorkflowRunPlan


def _roster() -> AgentTurnRoster:
    return AgentTurnRoster(
        entries=(
            AgentRosterEntry(
                AgentDefinition(
                    agent_id="reviewer000",
                    scope=AgentScope.PROJECT,
                    name="Reviewer",
                    description="Reviews one focused result.",
                    instructions="Investigate carefully and report evidence.",
                )
            ),
        )
    )


def _plan() -> WorkflowRunPlan:
    graph = WorkflowGraph(
        graph_id="workflow000",
        scope=AgentScope.PROJECT,
        name="Saved team",
    )
    return WorkflowRunPlan(
        graph_id=graph.graph_id,
        scope=graph.scope,
        name=graph.name,
        description="",
        provider="deepseek",
        graph=graph,
    )


def _explicit_target(key: str = "local-fast") -> AgentModelTarget:
    return AgentModelTarget(
        key=key,
        provider="local_openai",
        model="qwen-coder",
        label="Local — Qwen Coder",
    )


def test_model_targets_always_include_inherit_first_and_preserve_order() -> None:
    local = _explicit_target()
    hosted = AgentModelTarget(
        key="hosted-strong",
        provider="openai",
        model="gpt-5.5",
        label="OpenAI — GPT-5.5",
    )

    targets = AgentModelTargets.freeze((local, hosted))

    assert targets.keys == (INHERIT_MODEL_TARGET_KEY, "local-fast", "hosted-strong")
    assert targets.get(INHERIT_MODEL_TARGET_KEY) is not None
    assert targets.get(INHERIT_MODEL_TARGET_KEY).inherits is True
    assert targets.get("local-fast") is local
    assert tuple(targets) == (targets.get("inherit"), local, hosted)


def test_supplied_inherit_is_canonicalized_to_the_first_row() -> None:
    inherited = AgentModelTarget("inherit", "", "", "Use Aura")
    explicit = _explicit_target()

    targets = AgentModelTargets((explicit, inherited))

    assert targets.targets == (inherited, explicit)
    assert targets.catalog_rows()[0] == {
        "key": "inherit",
        "provider": "",
        "model": "",
        "label": "Use Aura",
    }


def test_duplicate_model_target_keys_are_rejected_after_normalization() -> None:
    with pytest.raises(ValueError, match="More than one.*duplicate"):
        AgentModelTargets(
            (
                _explicit_target("duplicate"),
                AgentModelTarget(
                    key=" duplicate ",
                    provider="openai",
                    model="gpt-5.5",
                ),
            )
        )


@pytest.mark.parametrize(
    "factory, message",
    [
        (lambda: AgentModelTarget("", "openai", "gpt-5.5"), "needs a key"),
        (
            lambda: AgentModelTarget("inherit", "openai", "gpt-5.5"),
            "may not name a provider",
        ),
        (
            lambda: AgentModelTarget("hosted", "openai", ""),
            "both a provider and model",
        ),
    ],
)
def test_invalid_model_target_shapes_are_rejected(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_catalog_projections_cannot_mutate_the_frozen_targets() -> None:
    targets = AgentModelTargets((_explicit_target(),))
    rows = targets.catalog_rows()
    rows[1]["model"] = "tampered"

    assert targets.get("local-fast").model == "qwen-coder"
    with pytest.raises(TypeError):
        targets.mapping["other"] = _explicit_target("other")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        targets.targets = ()  # type: ignore[misc]


def test_closed_turn_context_constructors_preserve_one_agent_path() -> None:
    roster = _roster()
    target = _explicit_target()
    plan = _plan()

    off = AgentTurnContext.off()
    automatic = AgentTurnContext.automatic(
        roster=roster,
        model_targets=(target,),
        root_provider="openai",
        root_model="gpt-root",
        root_thinking="high",
    )
    active = AgentTurnContext.active_workflow(plan)

    assert off.mode is AgentTurnMode.OFF
    assert off.enabled is False
    assert off.roster.is_empty
    assert off.workflow_plan is None

    assert automatic.mode is AgentTurnMode.AUTOMATIC
    assert automatic.enabled is True
    assert automatic.is_automatic is True
    assert automatic.roster is roster
    assert automatic.workflow_plan is None
    assert automatic.model_targets.get("local-fast") is target
    assert automatic.root_provider == "openai"
    assert automatic.root_model == "gpt-root"
    assert automatic.root_thinking == "high"

    assert active.mode is AgentTurnMode.ACTIVE_WORKFLOW
    assert active.enabled is True
    assert active.has_active_workflow is True
    assert active.roster.is_empty
    assert active.workflow_plan is plan
    assert active.model_targets.keys == ("inherit",)


@pytest.mark.parametrize(
    "factory, message",
    [
        (
            lambda: AgentTurnContext(
                mode=AgentTurnMode.OFF,
                roster=_roster(),
            ),
            "off Agent turn",
        ),
        (
            lambda: AgentTurnContext(
                mode=AgentTurnMode.AUTOMATIC,
                workflow_plan=_plan(),
            ),
            "automatic Agent turn",
        ),
        (
            lambda: AgentTurnContext(mode=AgentTurnMode.ACTIVE_WORKFLOW),
            "needs a frozen workflow plan",
        ),
        (
            lambda: AgentTurnContext(
                mode=AgentTurnMode.ACTIVE_WORKFLOW,
                roster=_roster(),
                workflow_plan=_plan(),
            ),
            "cannot also expose an automatic roster",
        ),
        (
            lambda: AgentTurnContext(
                mode=AgentTurnMode.OFF,
                model_targets=AgentModelTargets((_explicit_target(),)),
            ),
            "cannot carry explicit model targets",
        ),
        (
            lambda: AgentTurnContext(
                mode=AgentTurnMode.OFF,
                root_provider="openai",
                root_model="gpt-root",
            ),
            "cannot carry a root model target",
        ),
        (
            lambda: AgentTurnContext(
                mode=AgentTurnMode.ACTIVE_WORKFLOW,
                workflow_plan=_plan(),
                root_provider="openai",
                root_model="gpt-root",
            ),
            "cannot carry an automatic root target",
        ),
    ],
)
def test_direct_context_construction_rejects_mixed_modes(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_context_is_frozen_and_accepts_a_valid_string_mode() -> None:
    context = AgentTurnContext(mode="automatic")  # type: ignore[arg-type]

    assert context.mode is AgentTurnMode.AUTOMATIC
    with pytest.raises(FrozenInstanceError):
        context.mode = AgentTurnMode.OFF  # type: ignore[misc]


def test_context_rejects_partial_root_targets_and_non_frozen_workflow_values() -> None:
    with pytest.raises(ValueError, match="captured together"):
        AgentTurnContext.automatic(root_provider="openai")

    with pytest.raises(TypeError, match="WorkflowRunPlan"):
        AgentTurnContext(
            mode=AgentTurnMode.ACTIVE_WORKFLOW,
            workflow_plan=object(),  # type: ignore[arg-type]
        )
