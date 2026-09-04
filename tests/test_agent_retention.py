"""Automatic Agent retention persists exact definitions, grants, and graphs."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError
from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentLocalState, AgentPermission
from aura.agents.models import AgentDefinition
from aura.agents.retention import AgentRetentionError, AgentTeamRetention
from aura.agents.roster import AgentRosterEntry, AgentTurnRoster
from aura.agents.store import AgentStore
from aura.agents.team_compiler import CompiledAgentTeam, compile_agent_team
from aura.agents.team_spec import (
    AgentTeamSpec,
    HandoffSpec,
    HelperSpec,
    NewAgentSpec,
    OccurrenceSpec,
)
from aura.agents.turn_context import AgentModelTargets


@pytest.fixture(autouse=True)
def _configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda _provider: True
    )


def _generated(
    alias: str, *, permission: AgentPermission = AgentPermission.READ_ONLY
) -> NewAgentSpec:
    return NewAgentSpec(
        alias=alias,
        name=alias.title(),
        description=f"Handles {alias} work.",
        instructions=f"Exact private instructions for {alias}.",
        model_target="inherit",
        thinking="high",
        permission=permission.value,
    )


def _mixed_team(
    existing: AgentDefinition,
    *,
    existing_permission: AgentPermission = AgentPermission.READ_WRITE,
) -> CompiledAgentTeam:
    spec = AgentTeamSpec(
        task="Review both branches, combine them, and use focused help if needed.",
        name="Mixed retained workflow",
        description="Two repeated reviews feed one existing builder.",
        new_agents=(
            _generated("reviewer"),
            _generated("helper", permission=AgentPermission.READ_WRITE),
        ),
        occurrences=(
            OccurrenceSpec("left", "reviewer", "Review the left branch."),
            OccurrenceSpec("right", "reviewer", "Review the right branch."),
            OccurrenceSpec("build", existing.agent_id, "Join both reviews."),
            OccurrenceSpec("help", "helper", "Check one writable detail."),
        ),
        handoffs=(
            HandoffSpec("task", "left"),
            HandoffSpec("task", "right"),
            HandoffSpec("left", "build"),
            HandoffSpec("right", "build"),
            HandoffSpec("build", "result"),
        ),
        helpers=(HelperSpec("build", "help"),),
    )
    compiled, errors = compile_agent_team(
        spec,
        roster=AgentTurnRoster(
            (
                AgentRosterEntry(
                    existing, permission=existing_permission
                ),
            )
        ),
        model_targets=AgentModelTargets(),
        provider="deepseek",
        model="deepseek-chat",
        thinking="off",
    )
    assert errors == ()
    assert compiled is not None
    return compiled


def _stores(tmp_path: Path):
    workspace = tmp_path / "workspace"
    agents = AgentStore(workspace, personal_dir=tmp_path / "agents")
    state = AgentLocalState(workspace, state_root=tmp_path / "state")
    workflows = AgentGraphStore(
        workspace,
        personal_dir=tmp_path / "workflows",
        agent_scopes=lambda: {
            row.agent_id: row.scope for row in agents.list_summaries() if row.valid
        },
    )
    retention = AgentTeamRetention(
        agents=agents,
        workflows=workflows,
        local_state=state,
    )
    return agents, state, workflows, retention


def test_save_agent_then_keep_mixed_team_preserves_exact_graph_and_permissions(
    tmp_path: Path,
) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine inputs without losing evidence.",
    )
    state.set_permission(existing.agent_id, AgentPermission.READ_WRITE)
    team = _mixed_team(existing)
    reviewer, helper = team.generated_definitions

    saved = retention.save_agent(team, reviewer.agent_id)
    assert saved.agent_ids == (reviewer.agent_id,)
    assert agents.get(reviewer.agent_id) == reviewer
    assert state.permission(reviewer.agent_id) is AgentPermission.READ_ONLY
    assert state.available_ids() == (reviewer.agent_id,)

    kept = retention.keep_team(team)
    assert kept.workflow_id == team.plan.graph_id
    assert agents.get(reviewer.agent_id) == reviewer
    assert agents.get(helper.agent_id) == helper
    assert state.permission(helper.agent_id) is AgentPermission.READ_WRITE
    assert state.permission(existing.agent_id) is AgentPermission.READ_WRITE
    assert state.available_ids() == (reviewer.agent_id,)

    # The repeated reviewer occurrence creates one definition, while the exact
    # graph round-trips every branch, join, helper, assignment, position, and id.
    assert [row.agent_id for row in agents.list_summaries()].count(reviewer.agent_id) == 1
    assert workflows.get(team.plan.graph_id) == team.plan.graph
    assert workflows.get(team.plan.graph_id).connections == team.plan.graph.connections
    assert workflows.get(team.plan.graph_id).nodes == team.plan.graph.nodes

    # Both actions are safely idempotent and reuse the exact saved artifacts.
    assert retention.save_agent(team, reviewer.agent_id).message == "Saved"
    assert retention.keep_team(team).message == "Kept"


def test_collision_refuses_every_write_without_overwriting(tmp_path: Path) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine inputs.",
    )
    team = _mixed_team(existing)
    generated = team.generated_definitions[0]
    collision = replace(generated, name="Different content")
    agents.create_supplied(collision)

    with pytest.raises(AgentRetentionError, match="different content"):
        retention.save_agent(team, generated.agent_id)
    with pytest.raises(AgentRetentionError, match="different content"):
        retention.keep_team(team)

    assert agents.get(generated.agent_id) == collision
    assert workflows.get(team.plan.graph_id) is None
    assert state.available_ids() == ()
    assert state.permission(generated.agent_id) is AgentPermission.READ_ONLY


def test_keep_team_of_existing_agents_reuses_exact_immutable_definition(
    tmp_path: Path,
) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PERSONAL,
        name="Existing Reviewer",
        description="Reviews both stages.",
        instructions="Review each assigned stage exactly.",
        provider="deepseek",
        model="deepseek-chat",
    )
    spec = AgentTeamSpec(
        task="Review two stages in order.",
        name="Repeated existing reviewer",
        description="One saved specialist performs two distinct occurrences.",
        occurrences=(
            OccurrenceSpec("first", existing.agent_id, "Review the first stage."),
            OccurrenceSpec("second", existing.agent_id, "Review the second stage."),
        ),
        handoffs=(
            HandoffSpec("task", "first"),
            HandoffSpec("first", "second"),
            HandoffSpec("second", "result"),
        ),
    )
    compiled, errors = compile_agent_team(
        spec,
        roster=AgentTurnRoster((AgentRosterEntry(existing),)),
        model_targets=AgentModelTargets(),
        provider="deepseek",
        model="deepseek-chat",
        thinking="off",
    )
    assert errors == () and compiled is not None

    kept = retention.keep_team(compiled)

    assert kept.agent_ids == ()
    assert workflows.get(compiled.plan.graph_id) == compiled.plan.graph
    assert agents.get(existing.agent_id) == existing
    assert state.available_ids() == ()


def test_changed_saved_definition_refuses_keep_without_cloning_or_overwrite(
    tmp_path: Path,
) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine the original inputs.",
    )
    team = _mixed_team(existing)
    changed = replace(existing, instructions="Different instructions after the run.")
    agents.update(changed)

    with pytest.raises(AgentRetentionError, match="changed after this run"):
        retention.keep_team(team)

    assert agents.get(existing.agent_id) == changed
    assert all(agents.get(item.agent_id) is None for item in team.generated_definitions)
    assert workflows.get(team.plan.graph_id) is None


@pytest.mark.parametrize(
    ("compiled_permission", "current_permission"),
    (
        (AgentPermission.READ_ONLY, AgentPermission.READ_WRITE),
        (AgentPermission.READ_WRITE, AgentPermission.READ_ONLY),
    ),
)
def test_changed_saved_permission_refuses_keep_without_rewriting_authority(
    tmp_path: Path,
    compiled_permission: AgentPermission,
    current_permission: AgentPermission,
) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine the original inputs.",
    )
    state.set_permission(existing.agent_id, compiled_permission)
    team = _mixed_team(existing, existing_permission=compiled_permission)
    state.set_permission(existing.agent_id, current_permission)

    with pytest.raises(
        AgentRetentionError, match="permission changed after this run"
    ) as caught:
        retention.keep_team(team)

    assert compiled_permission.label in str(caught.value)
    assert current_permission.label in str(caught.value)
    assert state.permission(existing.agent_id) is current_permission
    assert all(agents.get(item.agent_id) is None for item in team.generated_definitions)
    assert workflows.get(team.plan.graph_id) is None


def test_changed_saved_generated_permission_is_not_reset_by_retention(
    tmp_path: Path,
) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine the original inputs.",
    )
    state.set_permission(existing.agent_id, AgentPermission.READ_WRITE)
    team = _mixed_team(existing)
    reviewer, helper = team.generated_definitions
    retention.save_agent(team, reviewer.agent_id)
    state.set_permission(reviewer.agent_id, AgentPermission.READ_WRITE)

    with pytest.raises(AgentRetentionError, match="permission changed after this run"):
        retention.save_agent(team, reviewer.agent_id)
    with pytest.raises(AgentRetentionError, match="permission changed after this run"):
        retention.keep_team(team)

    assert state.permission(reviewer.agent_id) is AgentPermission.READ_WRITE
    assert agents.get(reviewer.agent_id) == reviewer
    assert agents.get(helper.agent_id) is None
    assert workflows.get(team.plan.graph_id) is None


def test_retry_repairs_a_missing_generated_permission_after_definition_write(
    tmp_path: Path,
) -> None:
    agents, state, _workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine the original inputs.",
    )
    team = _mixed_team(existing)
    helper = team.generated_definitions[1]
    agents.create_supplied(helper)

    assert state.explicit_permission(helper.agent_id) is None
    saved = retention.save_agent(team, helper.agent_id)

    assert saved.agent_ids == (helper.agent_id,)
    assert state.permission(helper.agent_id) is AgentPermission.READ_WRITE
    assert state.available_ids() == (helper.agent_id,)


def test_workflow_id_collision_refuses_before_saving_generated_members(
    tmp_path: Path,
) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine inputs.",
    )
    state.set_permission(existing.agent_id, AgentPermission.READ_WRITE)
    team = _mixed_team(existing)
    collision = replace(team.plan.graph, name="Different saved Workflow")
    workflows.create_supplied(collision)

    with pytest.raises(AgentRetentionError, match="different content"):
        retention.keep_team(team)

    assert workflows.get(team.plan.graph_id) == collision
    assert all(agents.get(item.agent_id) is None for item in team.generated_definitions)
    assert state.available_ids() == ()


def test_partial_failure_is_reported_and_retry_reuses_identical_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents, state, workflows, retention = _stores(tmp_path)
    existing = agents.create(
        AgentScope.PROJECT,
        name="Existing Builder",
        description="Combines review results.",
        instructions="Combine inputs.",
    )
    state.set_permission(existing.agent_id, AgentPermission.READ_WRITE)
    team = _mixed_team(existing)
    original_create = workflows.create_supplied
    failures = 0

    def fail_once(graph):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise AgentGraphStoreError("simulated final write failure")
        return original_create(graph)

    monkeypatch.setattr(workflows, "create_supplied", fail_once)
    with pytest.raises(AgentRetentionError, match="simulated final write failure"):
        retention.keep_team(team)

    assert workflows.get(team.plan.graph_id) is None
    assert all(agents.get(item.agent_id) == item for item in team.generated_definitions)
    assert retention.keep_team(team).workflow_id == team.plan.graph_id
    assert workflows.get(team.plan.graph_id) == team.plan.graph
