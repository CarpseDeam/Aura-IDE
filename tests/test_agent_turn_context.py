"""The frozen Agent turn context owns the complete OFF/ENABLED gate."""

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
    AgentWorkflowCatalog,
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


def test_off_and_enabled_contexts_keep_all_enabled_routes_together() -> None:
    roster = _roster()
    target = _explicit_target()
    plan = _plan()

    off = AgentTurnContext.off()
    enabled = AgentTurnContext.enabled(
        roster=roster,
        workflows=(plan,),
        model_targets=(target,),
        root_provider="openai",
        root_model="gpt-root",
        root_thinking="high",
    )
    assert off.mode is AgentTurnMode.OFF
    assert off.is_enabled is False
    assert off.roster.is_empty
    assert len(off.workflows) == 0

    assert enabled.mode is AgentTurnMode.ENABLED
    assert enabled.is_enabled is True
    assert enabled.roster is roster
    assert enabled.workflow(plan.graph_id) is plan
    assert enabled.model_targets.get("local-fast") is target
    assert enabled.root_provider == "openai"
    assert enabled.root_model == "gpt-root"
    assert enabled.root_thinking == "high"


def test_workflow_catalog_is_frozen_concise_and_resolves_only_exact_ids() -> None:
    plan = _plan()
    catalog = AgentWorkflowCatalog.freeze((plan,))

    rows = catalog.catalog_rows()
    rows[0]["name"] = "tampered"

    assert catalog.ids == (plan.graph_id,)
    assert catalog.get(plan.graph_id) is plan
    assert catalog.get("unknownworkflow") is None
    assert catalog.catalog_rows() == (
        {
            "workflow_id": plan.graph_id,
            "name": plan.name,
            "description": plan.description,
        },
    )
    with pytest.raises(FrozenInstanceError):
        catalog.plans = ()  # type: ignore[misc]


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
                mode=AgentTurnMode.OFF,
                workflows=AgentWorkflowCatalog((_plan(),)),
            ),
            "off Agent turn",
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
    ],
)
def test_direct_context_construction_rejects_mixed_modes(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_context_is_frozen_and_accepts_a_valid_string_mode() -> None:
    context = AgentTurnContext(mode="enabled")  # type: ignore[arg-type]

    assert context.mode is AgentTurnMode.ENABLED
    with pytest.raises(FrozenInstanceError):
        context.mode = AgentTurnMode.OFF  # type: ignore[misc]


def test_context_rejects_partial_root_targets_and_non_frozen_workflow_values() -> None:
    with pytest.raises(ValueError, match="captured together"):
        AgentTurnContext.enabled(root_provider="openai")

    with pytest.raises(TypeError, match="AgentWorkflowCatalog"):
        AgentTurnContext(
            mode=AgentTurnMode.ENABLED,
            workflows=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError, match="WorkflowRunPlan"):
        AgentWorkflowCatalog((object(),))  # type: ignore[arg-type]
