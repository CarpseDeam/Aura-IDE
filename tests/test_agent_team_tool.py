"""The root automatic-team tool compiles once into the native workflow runner."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from aura.agents.delegation import DelegationUsage
from aura.agents.graph_models import WorkflowGraph
from aura.agents.identity import AgentScope
from aura.agents.models import AgentDefinition
from aura.agents.roster import AgentRosterEntry, AgentTurnRoster
from aura.agents.turn_context import AgentModelTarget, AgentTurnContext
from aura.agents.workflow_plan import WorkflowRunPlan
from aura.agents.workflow_runner import (
    WorkflowRunResult,
    WorkflowRunStatus,
    WorkflowUsageGroup,
)
from aura.conversation.tools.registry import ToolRegistry


def _names(registry: ToolRegistry) -> set[str]:
    return {tool["function"]["name"] for tool in registry.tool_defs()}


def _context(*, roster: AgentTurnRoster | None = None) -> AgentTurnContext:
    return AgentTurnContext.automatic(
        roster=roster or AgentTurnRoster(),
        model_targets=(
            AgentModelTarget(
                key="local-review",
                provider="local_openai",
                model="qwen-coder",
                label="Local review model",
            ),
        ),
        root_provider="deepseek",
        root_model="deepseek-chat",
        root_thinking="high",
    )


def _payload(*, permission: str = "read_only") -> dict:
    return {
        "task": "Inspect the requested behavior and return one result.",
        "team_name": "Focused review team",
        "team_description": "One specialist is enough for this bounded task.",
        "new_agents": [
            {
                "alias": "reviewer",
                "name": "Focused Reviewer",
                "description": "Reviews the requested behavior for concrete defects.",
                "instructions": "Inspect carefully and report only evidence-backed findings.",
                "model_target": "inherit",
                "thinking": "high",
                "permission": permission,
            }
        ],
        "occurrences": [
            {
                "alias": "review",
                "agent_ref": "reviewer",
                "assignment": "Review the behavior and produce the final findings.",
            }
        ],
        "handoffs": [
            {"source": "task", "target": "review"},
            {"source": "review", "target": "result"},
        ],
        "helpers": [],
    }


@dataclass
class _Runner:
    calls: list[tuple[WorkflowRunPlan, str, object]] = field(default_factory=list)

    def run(self, plan, task, *, cancel_event=None):
        self.calls.append((plan, task, cancel_event))
        return WorkflowRunResult(
            status=WorkflowRunStatus.COMPLETED,
            graph_id=plan.graph_id,
            workflow_name=plan.name,
            result="review complete",
            usage_groups=(
                WorkflowUsageGroup(
                    provider="deepseek",
                    model="deepseek-chat",
                    usage=DelegationUsage(prompt_tokens=12, completion_tokens=4),
                ),
            ),
        )


@pytest.fixture(autouse=True)
def _configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda _provider: True
    )


def test_agent_turn_modes_expose_exactly_one_root_agent_path(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    assert not (_names(registry) & {"delegate_agent", "run_agent_team", "run_agent_workflow"})

    definition = AgentDefinition(
        agent_id="existingreviewer1",
        scope=AgentScope.PROJECT,
        name="Existing Reviewer",
        description="Reviews a focused result.",
        instructions="Review the work carefully.",
    )
    context = _context(
        roster=AgentTurnRoster((AgentRosterEntry(definition=definition),))
    )
    registry.set_agent_turn_context(context)

    names = _names(registry)
    assert "run_agent_team" in names
    assert "delegate_agent" not in names
    assert "run_agent_workflow" not in names
    rendered = json.dumps(
        next(
            tool
            for tool in registry.tool_defs()
            if tool["function"]["name"] == "run_agent_team"
        )
    )
    assert "existingreviewer1" in rendered
    assert "local-review" in rendered
    assert definition.instructions not in rendered

    plan = WorkflowRunPlan(
        graph_id="savedworkflow1",
        scope=AgentScope.PROJECT,
        name="Saved workflow",
        description="The exact saved team.",
        provider="deepseek",
        graph=WorkflowGraph(
            graph_id="savedworkflow1",
            scope=AgentScope.PROJECT,
            name="Saved workflow",
        ),
    )
    registry.set_agent_turn_context(AgentTurnContext.active_workflow(plan))

    names = _names(registry)
    assert "run_agent_workflow" in names
    assert "run_agent_team" not in names
    assert "delegate_agent" not in names


def test_valid_team_runs_once_through_existing_runner_and_forwards_usage(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(tmp_path)
    context = _context()
    runner = _Runner()
    registry.set_agent_turn_context(context)
    registry.set_agent_workflow_runner(runner)

    first = registry.execute(
        "run_agent_team", _payload(), approval_cb=lambda _request: None
    )
    second = registry.execute(
        "run_agent_team", _payload(), approval_cb=lambda _request: None
    )

    assert first.ok is True
    assert first.payload["tool"] == "run_agent_team"
    assert first.payload["result"] == "review complete"
    assert first.payload["assembled"] is True
    assert first.payload["team"]["name"] == "Focused review team"
    assert "workflow" not in first.payload
    assert first.extras["delegation_usage_groups"] == [
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
        }
    ]
    assert len(runner.calls) == 1
    plan, task, cancel_event = runner.calls[0]
    assert plan.graph is not None
    assert task == _payload()["task"]
    assert cancel_event is None

    assert second.ok is False
    assert second.payload["failure_class"] == "agent_team_already_started"

    # A new submitted turn owns a fresh ledger, even if its immutable facts
    # happen to be equal to the preceding turn's.
    registry.set_agent_turn_context(context)
    third = registry.execute(
        "run_agent_team", _payload(), approval_cb=lambda _request: None
    )
    assert third.ok is True
    assert len(runner.calls) == 2


def test_invalid_shape_does_not_consume_the_turns_single_launch(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    runner = _Runner()
    registry.set_agent_turn_context(_context())
    registry.set_agent_workflow_runner(runner)
    invalid = _payload()
    invalid["handoffs"] = []

    refused = registry.execute(
        "run_agent_team", invalid, approval_cb=lambda _request: None
    )
    accepted = registry.execute(
        "run_agent_team", _payload(), approval_cb=lambda _request: None
    )

    assert refused.ok is False
    assert refused.payload["failure_class"] == "invalid_agent_team"
    assert refused.payload["errors"]
    assert accepted.ok is True
    assert len(runner.calls) == 1


@pytest.mark.parametrize("plan_review", [False, True])
def test_writable_team_is_refused_without_downgrading_root_authority(
    tmp_path: Path,
    plan_review: bool,
) -> None:
    registry = ToolRegistry(tmp_path, read_only=not plan_review)
    runner = _Runner()
    registry.set_agent_turn_context(_context())
    registry.set_agent_workflow_runner(runner)
    if plan_review:
        registry.plan_review.begin_turn(required=True)

    refused = registry.execute(
        "run_agent_team",
        _payload(permission="read_write"),
        approval_cb=lambda _request: None,
    )

    assert refused.ok is False
    assert refused.payload["failure_class"] == "root_mutation_forbidden"
    assert runner.calls == []

    # Policy refusal is not a hidden downgrade and does not spend the one run;
    # the root may correct the team to match its frozen authority.
    accepted = registry.execute(
        "run_agent_team", _payload(), approval_cb=lambda _request: None
    )
    assert accepted.ok is True
    assert len(runner.calls) == 1


def test_automatic_mode_preexposes_change_set_controls(tmp_path: Path) -> None:
    class _Worktrees:
        has_unresolved = False

    registry = ToolRegistry(tmp_path)
    registry.set_agent_worktree_manager(_Worktrees())
    assert "apply_agent_change_set" not in _names(registry)

    registry.set_agent_turn_context(_context())
    names = _names(registry)

    assert {
        "list_agent_change_sets",
        "inspect_agent_change_set",
        "apply_agent_change_set",
        "discard_agent_change_set",
    } <= names


def test_direct_team_call_fails_closed_when_automatic_mode_is_off(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(tmp_path)

    result = registry.execute(
        "run_agent_team", _payload(), approval_cb=lambda _request: None
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "agent_team_unavailable"
