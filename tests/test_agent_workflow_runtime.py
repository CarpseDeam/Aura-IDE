"""Frozen Agent workflow plans, serial execution, and turn-tool exposure."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

from aura.agents.delegation import DelegationFailure, DelegationResult, DelegationStatus
from aura.agents.graph_models import (
    ConnectionKind,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
)
from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentDefinition, AgentThinking
from aura.agents.workflow_plan import WorkflowRunPlan, freeze_workflow_plan
from aura.agents.workflow_runner import (
    WorkflowRunner,
    WorkflowRunStatus,
    WorkflowStepState,
)
from aura.agents.worktree import AgentChangeSet, AgentWorktree
from aura.conversation.tools.registry import ToolRegistry

AGENT_IDS = ("agentone0001", "agenttwo0002", "agentthree03")


class _Definitions:
    def __init__(self, definitions: tuple[AgentDefinition, ...]) -> None:
        self.items = {item.agent_id: item for item in definitions}

    def get(self, agent_id: str) -> AgentDefinition | None:
        return self.items.get(agent_id)


class _Permissions:
    def __init__(self, values: dict[str, AgentPermission]) -> None:
        self.values = values

    def permission(self, agent_id: str) -> AgentPermission:
        return self.values.get(agent_id, AgentPermission.READ_ONLY)


class _Child:
    def __init__(self, results: list[DelegationResult | Exception]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def run(
        self,
        entry,
        task,
        resolved,
        cancel_event,
        *,
        workspace_root,
        permission,
        worktree=None,
        workflow_step=False,
    ):
        self.calls.append(
            {
                "agent_id": entry.agent_id,
                "task": task,
                "resolved": resolved,
                "cancel_event": cancel_event,
                "workspace_root": workspace_root,
                "permission": permission,
                "worktree": worktree,
                "workflow_step": workflow_step,
            }
        )
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result, ()


class _Worktrees:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.created: list[str] = []
        self.recovered: list[AgentWorktree] = []

    def create(self, owner: str) -> AgentWorktree:
        self.created.append(owner)
        return AgentWorktree("aw-workflow", owner, "base", "refs/heads/run", self.path)

    def recover(self, worktree: AgentWorktree) -> AgentChangeSet:
        self.recovered.append(worktree)
        return AgentChangeSet(
            status="ready",
            change_set_id=worktree.change_set_id,
            agent_id=worktree.agent_id,
            base_sha=worktree.base_sha,
            result_sha="result",
            changed_paths=("changed.txt",),
            diffstat="1 file changed",
        )

    def set_workspace_root(self, root) -> None:
        self.workspace_root = root


def _definition(
    agent_id: str,
    name: str,
    *,
    model: str = "",
    thinking: AgentThinking = AgentThinking.INHERIT,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        scope=AgentScope.PROJECT,
        name=name,
        description=f"{name} does one workflow step.",
        instructions=f"Work as {name}.",
        model=model,
        thinking=thinking,
    )


def _graph(count: int = 2) -> WorkflowGraph:
    task = WorkflowNode("task", WorkflowNodeKind.TASK)
    result = WorkflowNode("result", WorkflowNodeKind.AURA_RESULT)
    steps = tuple(
        WorkflowNode(
            f"step{index}",
            WorkflowNodeKind.AGENT,
            agent_id=AGENT_IDS[index - 1],
            assignment=f"Assignment {index}",
        )
        for index in range(1, count + 1)
    )
    ordered = (task, *steps, result)
    edges = tuple(
        WorkflowConnection(
            f"edge{index}",
            ConnectionKind.STEP,
            ordered[index].node_id,
            ordered[index + 1].node_id,
            index,
        )
        for index in range(len(ordered) - 1)
    )
    return WorkflowGraph(
        graph_id="workflowplan1",
        scope=AgentScope.PROJECT,
        name="Release workflow",
        description="Review then summarize.",
        nodes=ordered,
        connections=edges,
    )


def _freeze(
    monkeypatch,
    graph: WorkflowGraph,
    permissions: dict[str, AgentPermission] | None = None,
) -> WorkflowRunPlan:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda _provider: True
    )
    definitions = tuple(
        _definition(
            agent_id,
            f"Agent {index}",
            model="explicit-model" if index == 2 else "",
            thinking=AgentThinking.MAX if index == 2 else AgentThinking.INHERIT,
        )
        for index, agent_id in enumerate(AGENT_IDS[: len(graph.nodes) - 2], start=1)
    )
    plan, errors = freeze_workflow_plan(
        graph,
        definitions=_Definitions(definitions),
        permissions=_Permissions(permissions or {}),
        agent_scopes={item.agent_id: item.scope for item in definitions},
        provider="deepseek",
        model="aura-current-model",
        thinking="high",
    )
    assert errors == ()
    assert plan is not None
    return plan


def _completed(agent_id: str, name: str, result: str) -> DelegationResult:
    return DelegationResult(
        status=DelegationStatus.COMPLETED,
        agent_id=agent_id,
        agent_name=name,
        result=result,
        provider="deepseek",
        model="model",
    )


def _tool_names(registry: ToolRegistry) -> set[str]:
    return {
        str((tool.get("function") or {}).get("name") or "")
        for tool in registry.tool_defs()
    }


def test_freezing_captures_graph_order_definitions_models_thinking_and_grants(
    monkeypatch,
) -> None:
    graph = _graph()
    permissions = {
        AGENT_IDS[0]: AgentPermission.READ_WRITE,
        AGENT_IDS[1]: AgentPermission.READ_ONLY,
    }
    plan = _freeze(monkeypatch, graph, permissions)

    edited = replace(
        graph,
        name="Edited later",
        nodes=tuple(
            replace(node, assignment="Changed later") if node.is_agent else node
            for node in graph.nodes
        ),
    )
    permissions[AGENT_IDS[0]] = AgentPermission.READ_ONLY

    assert edited != plan.graph
    assert plan.graph is graph
    assert plan.name == "Release workflow"
    assert [step.node_id for step in plan.steps] == ["step1", "step2"]
    assert [step.assignment for step in plan.steps] == ["Assignment 1", "Assignment 2"]
    assert [step.agent_name for step in plan.steps] == ["Agent 1", "Agent 2"]
    assert [step.permission for step in plan.steps] == [
        AgentPermission.READ_WRITE,
        AgentPermission.READ_ONLY,
    ]
    assert [(step.resolved.provider, step.resolved.model) for step in plan.steps] == [
        ("deepseek", "aura-current-model"),
        ("deepseek", "explicit-model"),
    ]
    assert [step.resolved.thinking for step in plan.steps] == ["high", "max"]


def test_runner_is_serial_and_hands_the_previous_structured_result_forward(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _graph(),
        {AGENT_IDS[0]: AgentPermission.READ_ONLY},
    )
    child = _Child(
        [
            _completed(AGENT_IDS[0], "Agent 1", "first answer"),
            _completed(AGENT_IDS[1], "Agent 2", "final answer"),
        ]
    )
    runner = WorkflowRunner(workspace_root=tmp_path, child=child)
    observed: list[tuple[str, WorkflowStepState]] = []

    result = runner.run(plan, "Original task", on_step=lambda *state: observed.append(state))

    assert result.status is WorkflowRunStatus.COMPLETED
    assert result.result == "final answer"
    assert [call["agent_id"] for call in child.calls] == list(AGENT_IDS[:2])
    assert "Original task" in child.calls[0]["task"]
    assert "Assignment 1" in child.calls[0]["task"]
    assert "Structured result" not in child.calls[0]["task"]
    assert "Assignment 2" in child.calls[1]["task"]
    assert '"status": "completed"' in child.calls[1]["task"]
    assert '"result": "first answer"' in child.calls[1]["task"]
    assert all(call["workflow_step"] is True for call in child.calls)
    assert observed == [
        ("step1", WorkflowStepState.RUNNING),
        ("step1", WorkflowStepState.SUCCEEDED),
        ("step2", WorkflowStepState.RUNNING),
        ("step2", WorkflowStepState.SUCCEEDED),
    ]


def test_writable_workflow_uses_one_shared_worktree_and_one_final_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    isolated = tmp_path / "isolated"
    worktrees = _Worktrees(isolated)
    plan = _freeze(
        monkeypatch,
        _graph(),
        {
            AGENT_IDS[0]: AgentPermission.READ_WRITE,
            AGENT_IDS[1]: AgentPermission.READ_ONLY,
        },
    )
    child = _Child(
        [
            _completed(AGENT_IDS[0], "Agent 1", "edited"),
            _completed(AGENT_IDS[1], "Agent 2", "reviewed"),
        ]
    )
    runner = WorkflowRunner(
        workspace_root=tmp_path, worktree_manager=worktrees, child=child
    )

    result = runner.run(plan, "Change and review")

    assert len(worktrees.created) == 1
    assert len(worktrees.recovered) == 1
    assert {call["workspace_root"] for call in child.calls} == {isolated}
    assert child.calls[0]["worktree"] is worktrees.recovered[0]
    assert child.calls[1]["worktree"] is None
    assert [call["permission"] for call in child.calls] == [
        AgentPermission.READ_WRITE,
        AgentPermission.READ_ONLY,
    ]
    assert result.change_set_id == "aw-workflow"
    assert result.result_sha == "result"
    assert result.changed_paths == ("changed.txt",)


def test_failure_stops_remaining_steps_and_recovers_existing_edits(
    tmp_path: Path, monkeypatch
) -> None:
    worktrees = _Worktrees(tmp_path / "isolated")
    plan = _freeze(
        monkeypatch,
        _graph(3),
        {AGENT_IDS[0]: AgentPermission.READ_WRITE},
    )
    failure = DelegationResult.failure(
        AGENT_IDS[1],
        DelegationFailure.PROVIDER_ERROR,
        "provider failed",
        agent_name="Agent 2",
    )
    child = _Child([_completed(AGENT_IDS[0], "Agent 1", "partial work"), failure])
    runner = WorkflowRunner(
        workspace_root=tmp_path, worktree_manager=worktrees, child=child
    )

    result = runner.run(plan, "Do three steps")

    assert result.status is WorkflowRunStatus.PARTIAL
    assert [outcome.state for outcome in result.steps] == [
        WorkflowStepState.SUCCEEDED,
        WorkflowStepState.FAILED,
        WorkflowStepState.SKIPPED,
    ]
    assert len(child.calls) == 2
    assert len(worktrees.recovered) == 1


def test_pre_start_cancellation_marks_a_step_and_checkpoints_once(
    tmp_path: Path, monkeypatch
) -> None:
    worktrees = _Worktrees(tmp_path / "isolated")
    plan = _freeze(
        monkeypatch,
        _graph(),
        {AGENT_IDS[0]: AgentPermission.READ_WRITE},
    )
    child = _Child([])
    cancel = threading.Event()
    cancel.set()
    runner = WorkflowRunner(
        workspace_root=tmp_path, worktree_manager=worktrees, child=child
    )

    result = runner.run(plan, "Cancelled task", cancel_event=cancel)

    assert result.status is WorkflowRunStatus.CANCELLED
    assert [outcome.state for outcome in result.steps] == [
        WorkflowStepState.CANCELLED,
        WorkflowStepState.SKIPPED,
    ]
    assert child.calls == []
    assert len(worktrees.recovered) == 1


def test_workflow_tool_and_its_copy_are_completely_absent_without_a_plan(
    tmp_path: Path, monkeypatch
) -> None:
    registry = ToolRegistry(tmp_path)
    without = json.dumps(registry.tool_defs())

    assert "run_agent_workflow" not in _tool_names(registry)
    assert "Release workflow" not in without

    plan = _freeze(monkeypatch, _graph())
    registry.set_turn_workflow_plan(plan)
    assert "run_agent_workflow" in _tool_names(registry)
    assert "Release workflow" in json.dumps(registry.tool_defs())

    registry.set_turn_workflow_plan(None)
    assert "run_agent_workflow" not in _tool_names(registry)
    assert "Release workflow" not in json.dumps(registry.tool_defs())
