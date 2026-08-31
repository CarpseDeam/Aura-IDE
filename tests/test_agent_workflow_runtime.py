"""Frozen Agent workflow plans, safe parallel waves, and turn-tool exposure."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from aura.agents.child_execution import ChildExecutor
from aura.agents.delegation import (
    DelegationFailure,
    DelegationResult,
    DelegationStatus,
    DelegationUsage,
)
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
from aura.agents.workflow_scheduler import WorkflowWaveScheduler
from aura.agents.worktree import AgentChangeSet, AgentWorktree
from aura.client import ContentDelta, Done, Event, Usage
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
        workflow_helpers=(),
        workflow_helper_runner=None,
        workflow_helper=False,
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
                "workflow_helpers": workflow_helpers,
                "workflow_helper_runner": workflow_helper_runner,
                "workflow_helper": workflow_helper,
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


class _ScriptedBackend:
    """One mutable backend session with a deterministic event script."""

    def __init__(
        self,
        rounds: list[list[Event]],
        *,
        cancel_on_request: int | None = None,
    ) -> None:
        self.rounds = rounds
        self.cancel_on_request = cancel_on_request
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any):
        self.requests.append(kwargs)
        index = len(self.requests) - 1
        if index == self.cancel_on_request:
            kwargs["cancel_event"].set()
        yield from self.rounds[index] if index < len(self.rounds) else []


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


def _dag_graph(
    steps: tuple[str, ...], edges: tuple[tuple[str, str], ...]
) -> WorkflowGraph:
    """Task and Aura Result, plus the named occurrences and the lines drawn."""
    agents = tuple(
        WorkflowNode(
            node_id,
            WorkflowNodeKind.AGENT,
            agent_id=AGENT_IDS[index],
            assignment=f"Assignment for {node_id}",
        )
        for index, node_id in enumerate(steps)
    )
    return WorkflowGraph(
        graph_id="workflowplan1",
        scope=AgentScope.PROJECT,
        name="Release workflow",
        description="Fan out, then join.",
        nodes=(
            WorkflowNode("task", WorkflowNodeKind.TASK),
            *agents,
            WorkflowNode("result", WorkflowNodeKind.AURA_RESULT),
        ),
        connections=tuple(
            WorkflowConnection(
                f"edge{index}", ConnectionKind.STEP, source, target, index
            )
            for index, (source, target) in enumerate(edges)
        ),
    )


def _with_helper(
    graph: WorkflowGraph,
    owner_node_id: str,
    *,
    node_id: str = "helper1",
    agent_id: str = AGENT_IDS[2],
    assignment: str = "Investigate the focused question.",
    connection_id: str | None = None,
) -> WorkflowGraph:
    helper = WorkflowNode(
        node_id,
        WorkflowNodeKind.AGENT,
        agent_id=agent_id,
        assignment=assignment,
    )
    return replace(
        graph,
        nodes=(*graph.nodes, helper),
        connections=(
            *graph.connections,
            WorkflowConnection(
                connection_id or f"to-{node_id}",
                ConnectionKind.SUB_AGENT,
                owner_node_id,
                node_id,
                len(graph.connections),
            ),
        ),
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


def _call(call_id: str, name: str, **args: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _tool_round(*calls: dict[str, Any]) -> list[Event]:
    return [
        Done(
            finish_reason="tool_calls",
            full_message={
                "role": "assistant",
                "content": None,
                "tool_calls": list(calls),
            },
        )
    ]


def _answer(text: str, *extra: Event) -> list[Event]:
    return [
        *extra,
        ContentDelta(text=text),
        Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": text},
        ),
    ]


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


def test_a_join_waits_for_every_branch_and_is_handed_them_in_frozen_order(
    tmp_path: Path, monkeypatch
) -> None:
    # Drawn right-to-join before left-to-join, so the bundle order is the
    # drawing's and not the order the branches happen to run in.
    graph = _dag_graph(
        ("left", "right", "join"),
        (
            ("task", "left"),
            ("task", "right"),
            ("right", "join"),
            ("left", "join"),
            ("join", "result"),
        ),
    )
    plan = _freeze(monkeypatch, graph, {AGENT_IDS[0]: AgentPermission.READ_WRITE})
    worktrees = _Worktrees(tmp_path / "isolated")
    child = _Child(
        [
            _completed(AGENT_IDS[0], "Agent 1", "left answer"),
            _completed(AGENT_IDS[1], "Agent 2", "right answer"),
            _completed(AGENT_IDS[2], "Agent 3", "joined answer"),
        ]
    )

    assert [step.node_id for step in plan.steps] == ["left", "right", "join"]
    assert plan.step("join").predecessors == ("right", "left")
    assert plan.step("left").successors == ("join",)
    assert plan.terminal_steps == (plan.step("join"),)
    assert plan.branched is True
    assert plan.catalog_row()["steps"][2]["after"] == ["Agent 2", "Agent 1"]

    result = WorkflowRunner(
        workspace_root=tmp_path, worktree_manager=worktrees, child=child
    ).run(plan, "Read it twice, then summarize")

    assert result.status is WorkflowRunStatus.COMPLETED
    assert result.result == "joined answer"
    assert [call["agent_id"] for call in child.calls] == list(AGENT_IDS)
    joined = child.calls[2]["task"]
    assert "Read it twice, then summarize" in joined
    assert "Assignment for join" in joined
    assert joined.index("right answer") < joined.index("left answer")
    assert '"position": 1' in joined and '"position": 2' in joined
    assert "Every predecessor succeeded." in joined
    assert "did not finish" not in joined
    # One shared worktree for the whole DAG, checkpointed exactly once.
    assert worktrees.created == ["workflow-workflowplan1"]
    assert len(worktrees.recovered) == 1
    assert result.change_set_id == "aw-workflow"


def test_an_independent_branch_finishes_after_its_sibling_fails(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("first", "second"),
            (
                ("task", "first"),
                ("task", "second"),
                ("first", "result"),
                ("second", "result"),
            ),
        ),
    )
    child = _Child(
        [
            DelegationResult.failure(
                AGENT_IDS[0],
                DelegationFailure.PROVIDER_ERROR,
                "provider failed",
                agent_name="Agent 1",
            ),
            _completed(AGENT_IDS[1], "Agent 2", "second answer"),
        ]
    )
    observed: list[tuple[str, WorkflowStepState]] = []

    result = WorkflowRunner(workspace_root=tmp_path, child=child).run(
        plan, "Two independent branches", on_step=lambda *args: observed.append(args)
    )

    assert result.status is WorkflowRunStatus.PARTIAL
    assert [outcome.state for outcome in result.steps] == [
        WorkflowStepState.FAILED,
        WorkflowStepState.SUCCEEDED,
    ]
    assert len(child.calls) == 2
    assert ("second", WorkflowStepState.RUNNING) in observed
    assert [branch.node_id for branch in result.branch_results] == ["first", "second"]
    assert [branch.state for branch in result.branch_results] == [
        WorkflowStepState.FAILED,
        WorkflowStepState.SUCCEEDED,
    ]
    payload = result.payload()
    assert [row["node_id"] for row in payload["branch_results"]] == ["first", "second"]
    assert "second answer" in payload["result"]
    assert payload["failure_class"] == DelegationFailure.PROVIDER_ERROR.value


def test_later_cancellation_overrides_an_earlier_independent_failure_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("first", "second"),
            (
                ("task", "first"),
                ("task", "second"),
                ("first", "result"),
                ("second", "result"),
            ),
        ),
    )
    failure = DelegationResult.failure(
        AGENT_IDS[0],
        DelegationFailure.PROVIDER_ERROR,
        "provider failed",
        agent_name="Agent 1",
    )

    class _FailThenCancel(_Child):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            args[3].set()
            return result

    child = _FailThenCancel([failure])
    cancel = threading.Event()

    result = WorkflowRunner(workspace_root=tmp_path, child=child).run(
        plan, "Two independent branches", cancel_event=cancel
    )

    assert result.status is WorkflowRunStatus.CANCELLED
    assert [outcome.state for outcome in result.steps] == [
        WorkflowStepState.FAILED,
        WorkflowStepState.CANCELLED,
    ]
    assert result.failure_class == "cancelled"
    assert result.error == "The run was stopped before this step started."
    assert result.steps[0].result.failure_class == DelegationFailure.PROVIDER_ERROR.value
    assert result.steps[0].result.error == "provider failed"
    assert cancel.is_set()
    assert child.calls[0]["cancel_event"] is cancel


def test_freezing_refuses_a_plan_with_a_visible_direct_bypass() -> None:
    graph = _graph()
    task = graph.task_node
    result = graph.result_node
    assert task is not None and result is not None
    graph = graph.with_connection(
        WorkflowConnection(
            "direct-bypass",
            ConnectionKind.STEP,
            task.node_id,
            result.node_id,
            len(graph.connections),
        )
    )
    definitions = tuple(
        _definition(agent_id, f"Agent {index}")
        for index, agent_id in enumerate(AGENT_IDS[:2], start=1)
    )

    plan, errors = freeze_workflow_plan(
        graph,
        definitions=_Definitions(definitions),
        permissions=_Permissions({}),
        agent_scopes={item.agent_id: item.scope for item in definitions},
        provider="deepseek",
        model="aura-current-model",
        thinking="high",
    )

    assert plan is None
    assert any("only valid for an empty workflow" in error for error in errors)


def test_a_join_is_blocked_truthfully_when_one_of_its_branches_fails(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("left", "right", "join"),
            (
                ("task", "left"),
                ("task", "right"),
                ("left", "join"),
                ("right", "join"),
                ("join", "result"),
            ),
        ),
    )
    child = _Child(
        [
            DelegationResult.failure(
                AGENT_IDS[0],
                DelegationFailure.PROVIDER_ERROR,
                "provider failed",
                agent_name="Agent 1",
            ),
            _completed(AGENT_IDS[1], "Agent 2", "right answer"),
        ]
    )

    result = WorkflowRunner(workspace_root=tmp_path, child=child).run(
        plan, "One branch will fail"
    )

    assert result.status is WorkflowRunStatus.PARTIAL
    assert [outcome.state for outcome in result.steps] == [
        WorkflowStepState.FAILED,
        WorkflowStepState.SUCCEEDED,
        WorkflowStepState.SKIPPED,
    ]
    # The sibling branch still ran; only the join was held back.
    assert [call["agent_id"] for call in child.calls] == list(AGENT_IDS[:2])
    blocked = result.steps[2]
    assert blocked.result.failure_class == DelegationFailure.DEPENDENCY_NOT_MET.value
    assert "Agent 1" in blocked.result.error
    assert blocked.payload()["blocked_by"] == ["left"]
    # One terminal branch, so nothing changes about how the answer is reported.
    assert "branch_results" not in result.payload()
    assert result.result == "right answer"


def test_several_terminal_branches_hand_aura_their_ordered_results(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("first", "second"),
            (
                ("task", "first"),
                ("first", "second"),
                ("first", "result"),
                ("second", "result"),
            ),
        ),
    )
    child = _Child(
        [
            _completed(AGENT_IDS[0], "Agent 1", "the first answer"),
            _completed(AGENT_IDS[1], "Agent 2", "the second answer"),
        ]
    )

    result = WorkflowRunner(workspace_root=tmp_path, child=child).run(
        plan, "Both ends report"
    )

    assert result.status is WorkflowRunStatus.COMPLETED
    assert [branch.node_id for branch in result.branch_results] == ["first", "second"]
    assert result.result.index("the first answer") < result.result.index(
        "the second answer"
    )
    assert "Agent 1" in result.result and "Agent 2" in result.result
    assert [row["agent_name"] for row in result.payload()["branch_results"]] == [
        "Agent 1",
        "Agent 2",
    ]


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


def test_helpers_freeze_per_occurrence_with_their_own_authority_and_identity(
    monkeypatch,
) -> None:
    graph = _graph(1)
    graph = _with_helper(
        graph,
        "step1",
        node_id="helper-a",
        agent_id=AGENT_IDS[1],
        assignment="Check the API boundary.",
        connection_id="dash-a",
    )
    graph = _with_helper(
        graph,
        "step1",
        node_id="helper-b",
        agent_id=AGENT_IDS[1],
        assignment="Check the persistence boundary.",
        connection_id="dash-b",
    )
    definitions = _Definitions(
        (
            _definition(AGENT_IDS[0], "Primary"),
            _definition(
                AGENT_IDS[1],
                "Reusable helper",
                model="helper-model",
                thinking=AgentThinking.MAX,
            ),
        )
    )
    permissions = _Permissions({AGENT_IDS[1]: AgentPermission.READ_WRITE})
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda _provider: True
    )

    plan, errors = freeze_workflow_plan(
        graph,
        definitions=definitions,
        permissions=permissions,
        agent_scopes={
            AGENT_IDS[0]: AgentScope.PROJECT,
            AGENT_IDS[1]: AgentScope.PROJECT,
        },
        provider="deepseek",
        model="primary-model",
        thinking="high",
    )
    assert errors == () and plan is not None

    permissions.values[AGENT_IDS[1]] = AgentPermission.READ_ONLY
    definitions.items[AGENT_IDS[1]] = _definition(AGENT_IDS[1], "Edited later")
    graph = replace(
        graph,
        nodes=tuple(
            replace(node, assignment="Edited after freeze")
            if node.node_id.startswith("helper-")
            else node
            for node in graph.nodes
        ),
    )

    primary = plan.steps[0]
    assert graph != plan.graph
    assert primary.permission is AgentPermission.READ_ONLY
    assert [helper.node_id for helper in primary.helpers] == ["helper-a", "helper-b"]
    assert [helper.connection_id for helper in primary.helpers] == ["dash-a", "dash-b"]
    assert [helper.owning_step_node_id for helper in primary.helpers] == [
        "step1",
        "step1",
    ]
    assert [helper.agent_id for helper in primary.helpers] == [
        AGENT_IDS[1],
        AGENT_IDS[1],
    ]
    assert [helper.assignment for helper in primary.helpers] == [
        "Check the API boundary.",
        "Check the persistence boundary.",
    ]
    assert all(
        helper.permission is AgentPermission.READ_WRITE for helper in primary.helpers
    )
    assert all(helper.agent_name == "Reusable helper" for helper in primary.helpers)
    assert all(helper.resolved.model == "helper-model" for helper in primary.helpers)
    assert all(helper.resolved.thinking == "max" for helper in primary.helpers)
    assert plan.writable is True
    assert plan.agent_ids == (AGENT_IDS[0], AGENT_IDS[1], AGENT_IDS[1])


def test_an_unused_writable_helper_still_allocates_the_worktree_before_the_step(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1), "step1", node_id="optional-writer", agent_id=AGENT_IDS[1]
        ),
        {AGENT_IDS[1]: AgentPermission.READ_WRITE},
    )
    worktrees = _Worktrees(tmp_path / "isolated")
    child = _Child([_completed(AGENT_IDS[0], "Agent 1", "no helper needed")])

    result = WorkflowRunner(
        workspace_root=tmp_path,
        worktree_manager=worktrees,
        child=child,
    ).run(plan, "Decide whether help is needed")

    assert result.status is WorkflowRunStatus.COMPLETED
    assert worktrees.created == ["workflow-workflowplan1"]
    assert len(worktrees.recovered) == 1
    assert len(child.calls) == 1
    assert child.calls[0]["workspace_root"] == tmp_path / "isolated"
    assert child.calls[0]["permission"] is AgentPermission.READ_ONLY
    assert child.calls[0]["worktree"] is None
    assert [helper.node_id for helper in child.calls[0]["workflow_helpers"]] == [
        "optional-writer"
    ]
    assert result.helper_invocations == ()


def test_only_the_owning_step_gets_helper_tool_and_prompt_weight(
    tmp_path: Path, monkeypatch
) -> None:
    graph = _with_helper(_graph(), "step1", node_id="step1-helper")
    plan = _freeze(monkeypatch, graph)
    backend = _ScriptedBackend([_answer("first"), _answer("second")])
    observed: list[tuple[str, WorkflowStepState]] = []
    runner = WorkflowRunner(
        workspace_root=tmp_path,
        backend_factory=lambda _provider: backend,
    )

    result = runner.run(plan, "Original task", on_step=lambda *args: observed.append(args))

    assert result.status is WorkflowRunStatus.COMPLETED
    assert result.helper_invocations == ()
    assert len(backend.requests) == 2  # the unused helper never ran
    first_tools = {
        tool["function"]["name"] for tool in backend.requests[0]["tools"]
    }
    second_tools = {
        tool["function"]["name"] for tool in backend.requests[1]["tools"]
    }
    assert "delegate_agent" in first_tools
    assert "delegate_agent" not in second_tools
    helper_schema = next(
        tool
        for tool in backend.requests[0]["tools"]
        if tool["function"]["name"] == "delegate_agent"
    )
    assert helper_schema["function"]["parameters"]["properties"][
        "helper_node_id"
    ]["enum"] == ["step1-helper"]
    helper_copy = helper_schema["function"]["description"]
    assert "existing shared worktree" in helper_copy
    assert "does not create or checkpoint another worktree" in helper_copy
    assert "Aura-owned branch" not in helper_copy
    first_prompt = backend.requests[0]["messages"][0]["content"]
    second_prompt = backend.requests[1]["messages"][0]["content"]
    assert "optional helpers listed in your delegate_agent tool" in first_prompt
    assert "You cannot delegate. There are no other agents" not in first_prompt
    assert "optional helpers listed in your delegate_agent tool" not in second_prompt
    assert "You cannot delegate. There are no other agents" in second_prompt
    assert all(node_id != "step1-helper" for node_id, _state in observed)


def test_writable_helper_uses_child_executor_and_the_one_shared_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    graph = _with_helper(
        _graph(1),
        "step1",
        node_id="writer-helper",
        agent_id=AGENT_IDS[1],
        assignment="Inspect and edit only if needed.",
        connection_id="writer-dash",
    )
    plan = _freeze(
        monkeypatch,
        graph,
        {AGENT_IDS[0]: AgentPermission.READ_ONLY, AGENT_IDS[1]: AgentPermission.READ_WRITE},
    )
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    worktrees = _Worktrees(isolated)
    primary_backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="writer-helper",
                    task="Inspect the persistence seam.",
                )
            ),
            _answer("primary incorporated the helper"),
        ]
    )
    helper_backend = _ScriptedBackend(
        [_answer("helper answer", Usage(11, 7, 3, 2))]
    )
    backends = iter((primary_backend, helper_backend))
    cancel = threading.Event()
    observed: list[tuple[str, WorkflowStepState]] = []
    runner = WorkflowRunner(
        workspace_root=tmp_path,
        worktree_manager=worktrees,
        backend_factory=lambda _provider: next(backends),
    )

    result = runner.run(
        plan,
        "Original workflow task",
        cancel_event=cancel,
        on_step=lambda *args: observed.append(args),
    )

    assert result.status is WorkflowRunStatus.COMPLETED
    assert result.result == "primary incorporated the helper"
    assert len(worktrees.created) == 1
    assert len(worktrees.recovered) == 1
    requests = [
        primary_backend.requests[0],
        helper_backend.requests[0],
        primary_backend.requests[1],
    ]
    assert all(request["cancel_event"] is cancel for request in requests)
    primary_tools = {
        tool["function"]["name"] for tool in primary_backend.requests[0]["tools"]
    }
    helper_tools = {
        tool["function"]["name"] for tool in helper_backend.requests[0]["tools"]
    }
    assert "apply_patch" not in primary_tools and "shell" not in primary_tools
    assert {"apply_patch", "shell"} <= helper_tools
    assert "delegate_agent" not in helper_tools
    assert "run_agent_workflow" not in helper_tools
    helper_messages = helper_backend.requests[0]["messages"]
    assert [message["role"] for message in helper_messages] == ["system", "user"]
    assert "assisting one specific Step" in helper_messages[0]["content"]
    assert "shared by\n  its Steps and writable helpers" in helper_messages[0]["content"]
    assert "Original workflow task" in helper_messages[1]["content"]
    assert "Inspect and edit only if needed." in helper_messages[1]["content"]
    assert "Inspect the persistence seam." in helper_messages[1]["content"]
    primary_continuation = json.dumps(primary_backend.requests[1]["messages"])
    assert "helper answer" in primary_continuation
    assert "writer-helper" in primary_continuation
    assert observed == [
        ("step1", WorkflowStepState.RUNNING),
        ("writer-helper", WorkflowStepState.RUNNING),
        ("writer-helper", WorkflowStepState.SUCCEEDED),
        ("step1", WorkflowStepState.SUCCEEDED),
    ]
    assert len(result.helper_invocations) == 1
    invocation = result.helper_invocations[0]
    assert invocation.owning_step_node_id == "step1"
    assert invocation.helper_node_id == "writer-helper"
    assert invocation.connection_id == "writer-dash"
    assert invocation.agent_id == AGENT_IDS[1]
    assert invocation.agent_name == "Agent 2"
    assert invocation.permission == AgentPermission.READ_WRITE.value
    assert invocation.state is WorkflowStepState.SUCCEEDED
    assert invocation.result.usage is not None
    assert invocation.result.usage.as_dict() == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "cache_hit_tokens": 3,
        "cache_miss_tokens": 2,
    }
    payload = result.payload()["helper_invocations"][0]
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "explicit-model"
    assert "change_set_id" not in payload
    assert result.payload()["change_set_id"] == "aw-workflow"
    assert "tool_calls" not in json.dumps(payload)


def test_multiple_calls_to_one_helper_are_all_retained(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1), "step1", node_id="repeat-helper", agent_id=AGENT_IDS[1]
        ),
    )
    primary_backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="repeat-helper",
                    task="First bounded check",
                )
            ),
            _tool_round(
                _call(
                    "help-2",
                    "delegate_agent",
                    helper_node_id="repeat-helper",
                    task="Second bounded check",
                )
            ),
            _answer("primary final"),
        ]
    )
    first_helper_backend = _ScriptedBackend([_answer("first helper result")])
    second_helper_backend = _ScriptedBackend([_answer("second helper result")])
    backends = iter(
        (primary_backend, first_helper_backend, second_helper_backend)
    )
    result = WorkflowRunner(
        workspace_root=tmp_path, backend_factory=lambda _provider: next(backends)
    ).run(plan, "Use the helper twice")

    assert result.status is WorkflowRunStatus.COMPLETED
    assert [item.invocation for item in result.helper_invocations] == [1, 2]
    assert [item.helper_node_id for item in result.helper_invocations] == [
        "repeat-helper",
        "repeat-helper",
    ]
    assert [item.result.result for item in result.helper_invocations] == [
        "first helper result",
        "second helper result",
    ]
    assert len(result.payload()["helper_invocations"]) == 2
    assert [
        len(first_helper_backend.requests[0]["messages"]),
        len(second_helper_backend.requests[0]["messages"]),
    ] == [2, 2]


def test_aura_triggered_workflow_uses_the_same_helper_runner_path(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1), "step1", node_id="aura-helper", agent_id=AGENT_IDS[1]
        ),
    )
    primary_backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="aura-helper",
                    task="Answer the bounded question",
                )
            ),
            _answer("workflow answer for Aura"),
        ]
    )
    helper_backend = _ScriptedBackend([_answer("answer for the Step")])
    backends = iter((primary_backend, helper_backend))
    runner = WorkflowRunner(
        workspace_root=tmp_path, backend_factory=lambda _provider: next(backends)
    )
    registry = ToolRegistry(tmp_path)
    registry.set_turn_workflow_plan(plan)
    registry.set_agent_workflow_runner(runner)

    tool_result = registry.execute(
        "run_agent_workflow",
        {"task": "Aura-triggered task"},
        approval_cb=lambda _request: None,
    )

    assert tool_result.ok is True
    assert tool_result.payload["result"] == "workflow answer for Aura"
    assert tool_result.payload["helper_invocations"][0][
        "helper_node_id"
    ] == "aura-helper"
    assert len(primary_backend.requests) == 2
    assert len(helper_backend.requests) == 1


def test_reused_nested_backend_fails_helper_without_stopping_the_workflow(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1), "step1", node_id="fallible-helper", agent_id=AGENT_IDS[1]
        ),
    )
    backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="fallible-helper",
                    task="Try the focused check",
                )
            ),
            _answer("primary handled the helper failure"),
        ]
    )
    observed: list[tuple[str, WorkflowStepState]] = []

    result = WorkflowRunner(
        workspace_root=tmp_path, backend_factory=lambda _provider: backend
    ).run(
        plan,
        "Keep going if the helper fails",
        on_step=lambda *args: observed.append(args),
    )

    assert result.status is WorkflowRunStatus.COMPLETED
    assert result.result == "primary handled the helper failure"
    assert len(backend.requests) == 2
    assert len(result.helper_invocations) == 1
    invocation = result.helper_invocations[0]
    assert invocation.state is WorkflowStepState.FAILED
    assert invocation.result.failure_class == DelegationFailure.INTERNAL_ERROR.value
    assert "reused by a nested child invocation" in invocation.result.error
    assert "internal_error" in json.dumps(backend.requests[1]["messages"])
    assert observed[-2:] == [
        ("fallible-helper", WorkflowStepState.FAILED),
        ("step1", WorkflowStepState.SUCCEEDED),
    ]


def test_workflow_cancellation_is_the_helper_cancellation_authority(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1), "step1", node_id="slow-helper", agent_id=AGENT_IDS[1]
        ),
    )
    primary_backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="slow-helper",
                    task="Wait for cancellation",
                )
            ),
        ]
    )
    helper_backend = _ScriptedBackend([[]], cancel_on_request=0)
    backends = iter((primary_backend, helper_backend))
    cancel = threading.Event()
    observed: list[tuple[str, WorkflowStepState]] = []

    result = WorkflowRunner(
        workspace_root=tmp_path, backend_factory=lambda _provider: next(backends)
    ).run(
        plan,
        "Cancel inside the helper",
        cancel_event=cancel,
        on_step=lambda *args: observed.append(args),
    )

    assert cancel.is_set()
    assert result.status is WorkflowRunStatus.CANCELLED
    assert len(primary_backend.requests) == 1
    assert len(helper_backend.requests) == 1
    assert all(
        request["cancel_event"] is cancel
        for request in primary_backend.requests + helper_backend.requests
    )
    assert result.helper_invocations[0].state is WorkflowStepState.CANCELLED
    assert result.steps[0].state is WorkflowStepState.CANCELLED
    assert observed == [
        ("step1", WorkflowStepState.RUNNING),
        ("slow-helper", WorkflowStepState.RUNNING),
        ("slow-helper", WorkflowStepState.CANCELLED),
        ("step1", WorkflowStepState.CANCELLED),
    ]


def test_root_read_only_and_plan_review_refuse_a_helper_writable_workflow(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1), "step1", node_id="writer-helper", agent_id=AGENT_IDS[1]
        ),
        {AGENT_IDS[1]: AgentPermission.READ_WRITE},
    )

    class _NeverRuns:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("the writable workflow must be refused before it starts")

    for read_only, plan_review in ((True, False), (False, True)):
        registry = ToolRegistry(tmp_path, read_only=read_only)
        runner = _NeverRuns()
        registry.set_turn_workflow_plan(plan)
        registry.set_agent_workflow_runner(runner)
        registry.plan_review.begin_turn(required=plan_review)

        tool_result = registry.execute(
            "run_agent_workflow",
            {"task": "Do not start"},
            approval_cb=lambda _request: None,
        )

        assert tool_result.ok is False
        assert (
            tool_result.payload["failure_class"]
            == DelegationFailure.ROOT_MUTATION_FORBIDDEN.value
        )
        assert runner.calls == 0


class _ForkingChild:
    """Fresh collaborator per invocation with intentionally shared test state."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def fork(self):
        return _ForkingChild(self._handler)

    def run(self, *args, **kwargs):
        return self._handler(*args, **kwargs), ()


def test_read_only_siblings_overlap_join_waits_and_projection_stays_frozen(
    tmp_path: Path, monkeypatch
) -> None:
    graph = _dag_graph(
        ("left", "right", "join"),
        (
            ("task", "left"),
            ("task", "right"),
            ("left", "join"),
            ("right", "join"),
            ("join", "result"),
        ),
    )
    graph = _with_helper(
        graph, "left", node_id="left-helper", connection_id="left-dash"
    )
    graph = _with_helper(
        graph, "right", node_id="right-helper", connection_id="right-dash"
    )
    plan = _freeze(monkeypatch, graph)
    siblings = threading.Barrier(2)
    right_finished = threading.Event()
    left_finished = threading.Event()
    join_started = threading.Event()
    completion_order: list[str] = []
    completion_lock = threading.Lock()

    def handle(entry, task, resolved, cancel_event, **kwargs):
        del resolved, cancel_event
        if kwargs.get("workflow_helper"):
            owner = "left" if "Agent 1" in task else "right"
            return _completed(entry.agent_id, entry.name, f"{owner} helper answer")
        if entry.agent_id in AGENT_IDS[:2]:
            siblings.wait(timeout=5)
            helper = kwargs["workflow_helpers"][0]
            kwargs["workflow_helper_runner"].run(helper, "Check this branch")
            if entry.agent_id == AGENT_IDS[1]:
                with completion_lock:
                    completion_order.append("right")
                right_finished.set()
                return _completed(entry.agent_id, entry.name, "right answer")
            assert right_finished.wait(timeout=5)
            with completion_lock:
                completion_order.append("left")
            left_finished.set()
            return _completed(entry.agent_id, entry.name, "left answer")
        assert right_finished.is_set() and left_finished.is_set()
        join_started.set()
        return _completed(entry.agent_id, entry.name, "joined answer")

    observed: list[tuple[str, WorkflowStepState]] = []
    observer_threads: set[int] = set()
    coordinator_thread = threading.get_ident()

    def observe(node_id: str, state: WorkflowStepState) -> None:
        observer_threads.add(threading.get_ident())
        observed.append((node_id, state))

    result = WorkflowRunner(
        workspace_root=tmp_path, child=_ForkingChild(handle)
    ).run(plan, "Inspect both branches", on_step=observe)

    assert join_started.is_set()
    assert completion_order == ["right", "left"]
    assert [outcome.node_id for outcome in result.steps] == ["left", "right", "join"]
    assert [item.owning_step_node_id for item in result.helper_invocations] == [
        "left",
        "right",
    ]
    assert [item.invocation for item in result.helper_invocations] == [1, 2]
    assert result.result == "joined answer"
    assert observer_threads == {coordinator_thread}
    assert plan.step("left").mutation_capable is False
    assert plan.step("right").mutation_capable is False
    running = [node_id for node_id, state in observed if state is WorkflowStepState.RUNNING]
    assert running.index("left") < running.index("right") < running.index("join")
    terminal = [
        node_id
        for node_id, state in observed
        if node_id in {"left", "right", "join"}
        and state is not WorkflowStepState.RUNNING
    ]
    assert terminal == ["right", "left", "join"]


def test_readers_and_writers_run_in_exclusive_frozen_waves(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("reader", "writer-one", "writer-two"),
            (
                ("task", "reader"),
                ("task", "writer-one"),
                ("task", "writer-two"),
                ("reader", "result"),
                ("writer-one", "result"),
                ("writer-two", "result"),
            ),
        ),
        {
            AGENT_IDS[1]: AgentPermission.READ_WRITE,
            AGENT_IDS[2]: AgentPermission.READ_WRITE,
        },
    )
    scheduler = WorkflowWaveScheduler(plan)
    assert [step.node_id for step in scheduler.decide({}).wave] == ["reader"]
    assert [
        step.node_id for step in scheduler.decide({"reader": True}).wave
    ] == ["writer-one"]
    assert [
        step.node_id
        for step in scheduler.decide({"reader": True, "writer-one": True}).wave
    ] == ["writer-two"]

    reader_started = threading.Event()
    reader_release = threading.Event()
    reader_finished = threading.Event()
    first_writer_started = threading.Event()
    first_writer_release = threading.Event()
    first_writer_finished = threading.Event()
    second_writer_started = threading.Event()

    def handle(entry, task, resolved, cancel_event, **kwargs):
        del task, resolved, cancel_event, kwargs
        if entry.agent_id == AGENT_IDS[0]:
            reader_started.set()
            assert reader_release.wait(timeout=5)
            reader_finished.set()
            return _completed(entry.agent_id, entry.name, "read")
        if entry.agent_id == AGENT_IDS[1]:
            assert reader_finished.is_set()
            first_writer_started.set()
            assert first_writer_release.wait(timeout=5)
            first_writer_finished.set()
            return _completed(entry.agent_id, entry.name, "write one")
        assert reader_finished.is_set() and first_writer_finished.is_set()
        second_writer_started.set()
        return _completed(entry.agent_id, entry.name, "write two")

    done = threading.Event()
    result_box: list[Any] = []
    worktrees = _Worktrees(tmp_path / "isolated")

    def run() -> None:
        try:
            result_box.append(
                WorkflowRunner(
                    workspace_root=tmp_path,
                    worktree_manager=worktrees,
                    child=_ForkingChild(handle),
                ).run(plan, "Read, then perform independent writes")
            )
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert reader_started.wait(timeout=5)
    assert not first_writer_started.is_set()
    reader_release.set()
    assert first_writer_started.wait(timeout=5)
    assert reader_finished.is_set() and not second_writer_started.is_set()
    first_writer_release.set()
    assert done.wait(timeout=5)
    thread.join()

    assert second_writer_started.is_set()
    assert result_box[0].status is WorkflowRunStatus.COMPLETED
    assert len(worktrees.recovered) == 1


def test_writable_helper_forces_exclusivity_but_read_only_helpers_do_not(
    tmp_path: Path, monkeypatch
) -> None:
    graph = _dag_graph(
        ("helper-owner", "reader"),
        (
            ("task", "helper-owner"),
            ("task", "reader"),
            ("helper-owner", "result"),
            ("reader", "result"),
        ),
    )
    graph = _with_helper(graph, "helper-owner", node_id="writer-helper")
    plan = _freeze(
        monkeypatch, graph, {AGENT_IDS[2]: AgentPermission.READ_WRITE}
    )
    owner = plan.step("helper-owner")
    assert owner.permission is AgentPermission.READ_ONLY
    assert owner.mutation_capable is True
    assert plan.step("reader").mutation_capable is False
    assert [step.node_id for step in WorkflowWaveScheduler(plan).decide({}).wave] == [
        "helper-owner"
    ]

    helper_started = threading.Event()
    helper_release = threading.Event()
    helper_finished = threading.Event()
    reader_started = threading.Event()

    def handle(entry, task, resolved, cancel_event, **kwargs):
        del task, resolved, cancel_event
        if kwargs.get("workflow_helper"):
            helper_started.set()
            assert helper_release.wait(timeout=5)
            helper_finished.set()
            return _completed(entry.agent_id, entry.name, "edited")
        if entry.agent_id == AGENT_IDS[0]:
            helper = kwargs["workflow_helpers"][0]
            kwargs["workflow_helper_runner"].run(helper, "Make the bounded edit")
            return _completed(entry.agent_id, entry.name, "owner done")
        assert helper_finished.is_set()
        reader_started.set()
        return _completed(entry.agent_id, entry.name, "reader done")

    done = threading.Event()
    worktrees = _Worktrees(tmp_path / "isolated")

    def run() -> None:
        try:
            WorkflowRunner(
                workspace_root=tmp_path,
                worktree_manager=worktrees,
                child=_ForkingChild(handle),
            ).run(plan, "Edit and read")
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert helper_started.wait(timeout=5)
    assert not reader_started.is_set()
    helper_release.set()
    assert done.wait(timeout=5)
    thread.join()
    assert reader_started.is_set()

    read_helper_plan = _freeze(
        monkeypatch,
        _with_helper(
            _dag_graph(
                ("one", "two"),
                (
                    ("task", "one"),
                    ("task", "two"),
                    ("one", "result"),
                    ("two", "result"),
                ),
            ),
            "one",
            node_id="reader-helper",
        ),
    )
    assert read_helper_plan.step("one").mutation_capable is False
    assert [
        step.node_id
        for step in WorkflowWaveScheduler(read_helper_plan).decide({}).wave
    ] == ["one", "two"]


def test_cancellation_starts_nothing_else_quiesces_and_checkpoints_once(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _graph(),
        {AGENT_IDS[0]: AgentPermission.READ_WRITE},
    )
    first_started = threading.Event()
    first_release = threading.Event()
    first_finished = threading.Event()
    second_started = threading.Event()
    cancel = threading.Event()

    class _CheckingWorktrees(_Worktrees):
        def recover(self, worktree):
            assert first_finished.is_set()
            return super().recover(worktree)

    def handle(entry, task, resolved, cancel_event, **kwargs):
        del task, resolved, kwargs
        if entry.agent_id == AGENT_IDS[0]:
            first_started.set()
            assert first_release.wait(timeout=5)
            first_finished.set()
            assert cancel_event is cancel and cancel_event.is_set()
            return DelegationResult(
                status=DelegationStatus.CANCELLED,
                agent_id=entry.agent_id,
                agent_name=entry.name,
                failure_class="cancelled",
                error="cancelled while active",
            )
        second_started.set()
        return _completed(entry.agent_id, entry.name, "must not run")

    done = threading.Event()
    result_box: list[Any] = []
    worktrees = _CheckingWorktrees(tmp_path / "isolated")

    def run() -> None:
        try:
            result_box.append(
                WorkflowRunner(
                    workspace_root=tmp_path,
                    worktree_manager=worktrees,
                    child=_ForkingChild(handle),
                ).run(plan, "Cancel safely", cancel_event=cancel)
            )
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert first_started.wait(timeout=5)
    cancel.set()
    assert not second_started.is_set()
    first_release.set()
    assert done.wait(timeout=5)
    thread.join()

    result = result_box[0]
    assert not second_started.is_set()
    assert [item.state for item in result.steps] == [
        WorkflowStepState.CANCELLED,
        WorkflowStepState.SKIPPED,
    ]
    assert len(worktrees.recovered) == 1


def test_shared_registry_is_serialized_without_permission_or_helper_scope_leaks(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1),
            "step1",
            node_id="scoped-helper",
            agent_id=AGENT_IDS[1],
        ),
    )
    shared_registry = ToolRegistry(tmp_path, read_only=True, isolated_agent=True)
    first_entered = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    factory_calls: list[int] = []
    registry_calls: list[int] = []
    second_registry_constructed = threading.Event()
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    class _Backend:
        def __init__(self, index: int) -> None:
            self.index = index

        def stream(self, **kwargs):
            with lock:
                requests.append(kwargs)
            if self.index == 0:
                first_entered.set()
                assert first_release.wait(timeout=5)
            else:
                second_entered.set()
            yield from _answer(f"answer {self.index}")

    def backend_factory(_provider: str):
        with lock:
            index = len(factory_calls)
            factory_calls.append(index)
        return _Backend(index)

    def registry_factory(_root: Path):
        with lock:
            registry_calls.append(len(registry_calls))
            if len(registry_calls) == 2:
                second_registry_constructed.set()
        return shared_registry

    prototype = ChildExecutor(
        backend_factory=backend_factory,
        registry_factory=registry_factory,
    )
    step = plan.steps[0]
    results: list[DelegationResult] = []

    def invoke(*, writable: bool, helpers=()) -> None:
        result, _tests = prototype.fork().run(
            step.entry,
            "isolated child",
            step.resolved,
            threading.Event(),
            workspace_root=tmp_path,
            permission=(
                AgentPermission.READ_WRITE if writable else AgentPermission.READ_ONLY
            ),
            workflow_step=True,
            workflow_helpers=helpers,
            workflow_helper_runner=object() if helpers else None,
        )
        results.append(result)

    first = threading.Thread(
        target=invoke, kwargs={"writable": True, "helpers": step.helpers}
    )
    second = threading.Thread(target=invoke, kwargs={"writable": False})
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    # The second invocation cannot even construct its backend until the first
    # releases the shared registry and clears its writable/helper context.
    assert second_registry_constructed.wait(timeout=5)
    assert factory_calls == [0]
    assert not second_entered.is_set()
    first_release.set()
    first.join()
    second.join()

    assert second_entered.is_set()
    assert len(results) == 2 and all(result.ok for result in results)
    first_tools = {tool["function"]["name"] for tool in requests[0]["tools"]}
    second_tools = {tool["function"]["name"] for tool in requests[1]["tools"]}
    assert {"apply_patch", "delegate_agent"} <= first_tools
    assert "apply_patch" not in second_tools
    assert "delegate_agent" not in second_tools
    assert "optional helpers listed" in requests[0]["messages"][0]["content"]
    assert "optional helpers listed" not in requests[1]["messages"][0]["content"]


def test_shared_backend_and_unforkable_child_are_serialized(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("one", "two"),
            (
                ("task", "one"),
                ("task", "two"),
                ("one", "result"),
                ("two", "result"),
            ),
        ),
    )
    (tmp_path / "note.txt").write_text("shared session\n", encoding="utf-8")
    tool_entered = threading.Event()
    tool_release = threading.Event()
    second_backend_factory = threading.Event()
    second_backend_entered = threading.Event()
    backend_factory_calls = 0
    backend_stream_calls = 0
    first_stream_calls = 0
    backend_lock = threading.Lock()

    class _SharedBackend:
        def stream(self, **kwargs):
            nonlocal backend_stream_calls, first_stream_calls
            first_child = "first child" in json.dumps(kwargs["messages"])
            with backend_lock:
                backend_stream_calls += 1
                if first_child:
                    first_stream_calls += 1
                    round_index = first_stream_calls
            if not first_child:
                second_backend_entered.set()
                yield from _answer("second answer")
            elif round_index == 1:
                yield Done(
                    "tool_calls",
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [_call("read", "read_file", path="note.txt")],
                    },
                )
            else:
                yield from _answer("first answer")

    class _BlockingRegistry(ToolRegistry):
        def execute(self, name, args, approval_cb, **kwargs):
            if name == "read_file":
                tool_entered.set()
                assert tool_release.wait(timeout=5)
            return super().execute(name, args, approval_cb, **kwargs)

    shared_backend = _SharedBackend()

    def backend_factory(_provider: str):
        nonlocal backend_factory_calls
        with backend_lock:
            backend_factory_calls += 1
            if backend_factory_calls == 2:
                second_backend_factory.set()
        return shared_backend

    prototype = ChildExecutor(
        backend_factory=backend_factory,
        registry_factory=lambda root: _BlockingRegistry(root),
    )
    step = plan.steps[0]

    def invoke(task: str) -> None:
        prototype.fork().run(
            step.entry,
            task,
            step.resolved,
            threading.Event(),
            workspace_root=tmp_path,
            permission=AgentPermission.READ_ONLY,
        )

    first = threading.Thread(target=invoke, args=("first child",))
    second = threading.Thread(target=invoke, args=("second child",))
    first.start()
    assert tool_entered.wait(timeout=5)
    second.start()
    assert second_backend_factory.wait(timeout=5)
    try:
        assert not second_backend_entered.wait(timeout=0.2)
    finally:
        tool_release.set()
    first.join()
    second.join()
    assert second_backend_entered.is_set()
    assert first_stream_calls == 2
    assert backend_stream_calls == 3

    child_entered = threading.Event()
    child_release = threading.Event()
    second_child_running = threading.Event()
    child_calls = 0
    child_lock = threading.Lock()

    class _UnforkableChild:
        def run(self, entry, task, resolved, cancel_event, **kwargs):
            del task, resolved, cancel_event, kwargs
            nonlocal child_calls
            with child_lock:
                index = child_calls
                child_calls += 1
            if index == 0:
                child_entered.set()
                assert child_release.wait(timeout=5)
            return _completed(entry.agent_id, entry.name, f"child {index}"), ()

    finished = threading.Event()

    def observe(node_id: str, state: WorkflowStepState) -> None:
        if node_id == "two" and state is WorkflowStepState.RUNNING:
            second_child_running.set()

    def run_workflow() -> None:
        try:
            WorkflowRunner(
                workspace_root=tmp_path, child=_UnforkableChild()
            ).run(plan, "serialize unsafe injection", on_step=observe)
        finally:
            finished.set()

    workflow_thread = threading.Thread(target=run_workflow)
    workflow_thread.start()
    assert child_entered.wait(timeout=5)
    assert second_child_running.wait(timeout=5)
    assert child_calls == 1
    child_release.set()
    assert finished.wait(timeout=5)
    workflow_thread.join()
    assert child_calls == 2


def test_nested_shared_registry_reuse_fails_closed_without_deadlock(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1),
            "step1",
            node_id="nested-helper",
            agent_id=AGENT_IDS[1],
        ),
    )
    shared_registry = ToolRegistry(tmp_path, read_only=True, isolated_agent=True)
    backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "nested-call",
                    "delegate_agent",
                    helper_node_id="nested-helper",
                    task="Try the isolated helper",
                )
            ),
            _answer("parent handled the isolated failure"),
        ]
    )

    result = WorkflowRunner(
        workspace_root=tmp_path,
        backend_factory=lambda _provider: backend,
        registry_factory=lambda _root: shared_registry,
    ).run(plan, "Do not reuse mutable helper scope")

    assert result.status is WorkflowRunStatus.COMPLETED
    assert result.result == "parent handled the isolated failure"
    assert len(result.helper_invocations) == 1
    assert (
        result.helper_invocations[0].result.failure_class
        == DelegationFailure.INTERNAL_ERROR.value
    )
    assert "cannot be isolated" in result.helper_invocations[0].result.error
    assert len(backend.requests) == 2


def test_worker_exception_becomes_structured_without_stopping_its_sibling(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("broken", "healthy"),
            (
                ("task", "broken"),
                ("task", "healthy"),
                ("broken", "result"),
                ("healthy", "result"),
            ),
        ),
    )
    siblings = threading.Barrier(2)

    def handle(entry, task, resolved, cancel_event, **kwargs):
        del task, resolved, cancel_event, kwargs
        siblings.wait(timeout=5)
        if entry.agent_id == AGENT_IDS[0]:
            raise RuntimeError("worker exploded with secret-safe detail")
        return _completed(entry.agent_id, entry.name, "healthy answer")

    result = WorkflowRunner(
        workspace_root=tmp_path, child=_ForkingChild(handle)
    ).run(plan, "Contain one worker exception")

    assert result.status is WorkflowRunStatus.PARTIAL
    assert result.steps[0].state is WorkflowStepState.FAILED
    assert (
        result.steps[0].result.failure_class
        == DelegationFailure.INTERNAL_ERROR.value
    )
    assert result.steps[1].state is WorkflowStepState.SUCCEEDED
    assert result.steps[1].result.result == "healthy answer"


def test_workflow_usage_groups_keep_provider_models_distinct_and_frozen(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(monkeypatch, _graph(3))
    first = replace(
        _completed(AGENT_IDS[0], "Agent 1", "first"),
        provider="deepseek",
        model="model-a",
        usage=DelegationUsage(10, 2, 3, 7),
    )
    second = replace(
        _completed(AGENT_IDS[1], "Agent 2", "second"),
        provider="deepseek",
        model="model-b",
        usage=DelegationUsage(20, 4, 5, 15),
    )
    third = replace(
        _completed(AGENT_IDS[2], "Agent 3", "third"),
        provider="deepseek",
        model="model-a",
        usage=DelegationUsage(30, 6, 7, 23),
    )
    registry = ToolRegistry(tmp_path)
    registry.set_turn_workflow_plan(plan)
    registry.set_agent_workflow_runner(
        WorkflowRunner(
            workspace_root=tmp_path, child=_Child([first, second, third])
        )
    )

    tool_result = registry.execute(
        "run_agent_workflow",
        {"task": "Account for both models"},
        approval_cb=lambda _request: None,
    )

    assert [
        (row["provider"], row["model"])
        for row in tool_result.extras["delegation_usage_groups"]
    ] == [("deepseek", "model-a"), ("deepseek", "model-b")]
    assert tool_result.extras["delegation_usage_groups"][0]["prompt_tokens"] == 40
    assert tool_result.extras["delegation_usage_groups"][1]["prompt_tokens"] == 20


def test_descendant_blocks_only_after_every_predecessor_settles(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _dag_graph(
            ("left", "right", "join"),
            (
                ("task", "left"),
                ("task", "right"),
                ("right", "join"),
                ("left", "join"),
                ("join", "result"),
            ),
        ),
    )
    siblings = threading.Barrier(2)
    left_failed = threading.Event()
    right_release = threading.Event()
    right_failed = threading.Event()
    join_settled = threading.Event()

    def handle(entry, task, resolved, cancel_event, **kwargs):
        del task, resolved, cancel_event, kwargs
        if entry.agent_id == AGENT_IDS[0]:
            siblings.wait(timeout=5)
            left_failed.set()
            return DelegationResult.failure(
                entry.agent_id,
                DelegationFailure.PROVIDER_ERROR,
                "left failed",
                agent_name=entry.name,
            )
        if entry.agent_id == AGENT_IDS[1]:
            siblings.wait(timeout=5)
            assert right_release.wait(timeout=5)
            right_failed.set()
            return DelegationResult.failure(
                entry.agent_id,
                DelegationFailure.EMPTY_RESULT,
                "right failed",
                agent_name=entry.name,
            )
        raise AssertionError("the blocked join must not launch")

    done = threading.Event()
    result_box: list[Any] = []

    def observe(node_id: str, state: WorkflowStepState) -> None:
        if node_id == "join" and state is WorkflowStepState.SKIPPED:
            join_settled.set()

    def run() -> None:
        try:
            result_box.append(
                WorkflowRunner(
                    workspace_root=tmp_path, child=_ForkingChild(handle)
                ).run(plan, "Both branches fail", on_step=observe)
            )
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert left_failed.wait(timeout=5)
    assert not right_failed.is_set()
    assert not join_settled.is_set()
    right_release.set()
    assert done.wait(timeout=5)
    thread.join()

    blocked = result_box[0].steps[2]
    assert join_settled.is_set()
    assert blocked.state is WorkflowStepState.SKIPPED
    assert blocked.payload()["blocked_by"] == ["right", "left"]


def test_workflow_tool_copy_states_parallel_exclusivity_join_and_order(
    tmp_path: Path, monkeypatch
) -> None:
    graph = _dag_graph(
        ("left", "right", "join"),
        (
            ("task", "left"),
            ("task", "right"),
            ("left", "join"),
            ("right", "join"),
            ("join", "result"),
        ),
    )
    graph = _with_helper(graph, "left", node_id="writer-helper")
    plan = _freeze(
        monkeypatch, graph, {AGENT_IDS[2]: AgentPermission.READ_WRITE}
    )
    registry = ToolRegistry(tmp_path)
    registry.set_turn_workflow_plan(plan)
    workflow_tool = next(
        tool
        for tool in registry.tool_defs()
        if tool["function"]["name"] == "run_agent_workflow"
    )
    description = workflow_tool["function"]["description"]

    assert "Independent ready read-only Steps may overlap" in description
    assert "Read / Write helper runs exclusively" in description
    assert "Joins therefore wait for every predecessor" in description
    assert "frozen workflow order, never completion order" in description
