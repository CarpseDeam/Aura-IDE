"""Frozen Agent workflow plans, serial execution, and turn-tool exposure."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

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
    """One event script shared by the Step and its synchronous helpers."""

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
    backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="writer-helper",
                    task="Inspect the persistence seam.",
                )
            ),
            _answer("helper answer", Usage(11, 7, 3, 2)),
            _answer("primary incorporated the helper"),
        ]
    )
    cancel = threading.Event()
    observed: list[tuple[str, WorkflowStepState]] = []
    runner = WorkflowRunner(
        workspace_root=tmp_path,
        worktree_manager=worktrees,
        backend_factory=lambda _provider: backend,
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
    assert len(backend.requests) == 3
    assert all(request["cancel_event"] is cancel for request in backend.requests)
    primary_tools = {
        tool["function"]["name"] for tool in backend.requests[0]["tools"]
    }
    helper_tools = {
        tool["function"]["name"] for tool in backend.requests[1]["tools"]
    }
    assert "apply_patch" not in primary_tools and "shell" not in primary_tools
    assert {"apply_patch", "shell"} <= helper_tools
    assert "delegate_agent" not in helper_tools
    assert "run_agent_workflow" not in helper_tools
    helper_messages = backend.requests[1]["messages"]
    assert [message["role"] for message in helper_messages] == ["system", "user"]
    assert "assisting one specific Step" in helper_messages[0]["content"]
    assert "shared by\n  its Steps and writable helpers" in helper_messages[0]["content"]
    assert "Original workflow task" in helper_messages[1]["content"]
    assert "Inspect and edit only if needed." in helper_messages[1]["content"]
    assert "Inspect the persistence seam." in helper_messages[1]["content"]
    primary_continuation = json.dumps(backend.requests[2]["messages"])
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
    backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="repeat-helper",
                    task="First bounded check",
                )
            ),
            _answer("first helper result"),
            _tool_round(
                _call(
                    "help-2",
                    "delegate_agent",
                    helper_node_id="repeat-helper",
                    task="Second bounded check",
                )
            ),
            _answer("second helper result"),
            _answer("primary final"),
        ]
    )
    result = WorkflowRunner(
        workspace_root=tmp_path, backend_factory=lambda _provider: backend
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
    assert [len(backend.requests[index]["messages"]) for index in (1, 3)] == [2, 2]


def test_aura_triggered_workflow_uses_the_same_helper_runner_path(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _freeze(
        monkeypatch,
        _with_helper(
            _graph(1), "step1", node_id="aura-helper", agent_id=AGENT_IDS[1]
        ),
    )
    backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="aura-helper",
                    task="Answer the bounded question",
                )
            ),
            _answer("answer for the Step"),
            _answer("workflow answer for Aura"),
        ]
    )
    runner = WorkflowRunner(
        workspace_root=tmp_path, backend_factory=lambda _provider: backend
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
    assert len(backend.requests) == 3


def test_helper_failure_returns_to_the_step_without_stopping_the_workflow(
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
            _answer(""),
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
    assert len(backend.requests) == 3
    assert len(result.helper_invocations) == 1
    invocation = result.helper_invocations[0]
    assert invocation.state is WorkflowStepState.FAILED
    assert invocation.result.failure_class == DelegationFailure.EMPTY_RESULT.value
    assert "empty_result" in json.dumps(backend.requests[2]["messages"])
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
    backend = _ScriptedBackend(
        [
            _tool_round(
                _call(
                    "help-1",
                    "delegate_agent",
                    helper_node_id="slow-helper",
                    task="Wait for cancellation",
                )
            ),
            [],
        ],
        cancel_on_request=1,
    )
    cancel = threading.Event()
    observed: list[tuple[str, WorkflowStepState]] = []

    result = WorkflowRunner(
        workspace_root=tmp_path, backend_factory=lambda _provider: backend
    ).run(
        plan,
        "Cancel inside the helper",
        cancel_event=cancel,
        on_step=lambda *args: observed.append(args),
    )

    assert cancel.is_set()
    assert result.status is WorkflowRunStatus.CANCELLED
    assert len(backend.requests) == 2
    assert all(request["cancel_event"] is cancel for request in backend.requests)
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
