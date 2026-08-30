"""The first runnable topology, and what it refuses.

Validation decides what is *marked*, never what survives. Every graph below —
branched, looping, half-drawn, pointing at an agent that no longer exists —
is still a graph, and would still save and still draw. What changes is
whether it could be run, and what the canvas says about the part that is
wrong.
"""
from __future__ import annotations

import pytest

from aura.agents.graph_models import (
    ConnectionKind,
    Point,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
    new_connection_id,
    new_graph,
    new_node_id,
)
from aura.agents.graph_validation import (
    MISSING_AGENT_LABEL,
    reference_scope_error,
    validate_graph,
)
from aura.agents.identity import AgentScope

AGENTS = {
    "reviewer0000": AgentScope.PROJECT,
    "scout0000000": AgentScope.PROJECT,
    "personalagent": AgentScope.PERSONAL,
}


def _agent(agent_id: str = "reviewer0000") -> WorkflowNode:
    return WorkflowNode(
        node_id=new_node_id(), kind=WorkflowNodeKind.AGENT, agent_id=agent_id
    )


def _edge(kind: ConnectionKind, source: str, target: str, order: int = 0):
    return WorkflowConnection(
        connection_id=new_connection_id(),
        kind=kind,
        source_id=source,
        target_id=target,
        order=order,
    )


def _straight(*agents: WorkflowNode) -> WorkflowGraph:
    """Task → each agent in turn → Aura Result, which is the runnable shape."""
    graph = new_graph(AgentScope.PROJECT, "Release review")
    task = graph.task_node
    result = graph.result_node
    assert task is not None and result is not None
    previous = task.node_id
    for index, node in enumerate(agents):
        graph = graph.with_node(node).with_connection(
            _edge(ConnectionKind.STEP, previous, node.node_id, index)
        )
        previous = node.node_id
    return graph.with_connection(
        _edge(ConnectionKind.STEP, previous, result.node_id, len(agents))
    )


# ── the shape that works ─────────────────────────────────────────────────────


def test_one_unbranched_path_from_task_to_result_is_runnable() -> None:
    graph = _straight(_agent(), _agent("scout0000000"))

    verdict = validate_graph(graph, agents=AGENTS)

    assert verdict.runnable is True
    assert verdict.messages == ()


def test_a_workflow_with_no_agents_between_the_ends_is_still_runnable() -> None:
    verdict = validate_graph(_straight(), agents=AGENTS)

    assert verdict.runnable is True


def test_a_brand_new_workflow_says_the_task_leads_nowhere_yet() -> None:
    verdict = validate_graph(new_graph(AgentScope.PROJECT, "Draft"), agents=AGENTS)

    assert verdict.runnable is False
    assert "does not lead to the Aura Result" in " ".join(verdict.messages)


# ── the two fixed ends ───────────────────────────────────────────────────────


def test_a_second_task_node_is_refused() -> None:
    graph = _straight(_agent()).with_node(
        WorkflowNode(node_id=new_node_id(), kind=WorkflowNodeKind.TASK)
    )

    verdict = validate_graph(graph, agents=AGENTS)

    assert any("exactly one Task node" in message for message in verdict.messages)


def test_a_workflow_with_no_aura_result_is_refused() -> None:
    graph = _straight(_agent())
    result = graph.result_node
    assert result is not None

    verdict = validate_graph(graph.without_node(result.node_id), agents=AGENTS)

    assert any("no Aura Result node" in message for message in verdict.messages)


def test_nothing_may_run_before_the_task_or_after_the_result() -> None:
    graph = _straight(_agent())
    task = graph.task_node
    result = graph.result_node
    assert task is not None and result is not None
    stray = _agent("scout0000000")
    graph = (
        graph.with_node(stray)
        .with_connection(_edge(ConnectionKind.STEP, stray.node_id, task.node_id, 9))
        .with_connection(_edge(ConnectionKind.STEP, result.node_id, stray.node_id, 10))
    )

    messages = " ".join(validate_graph(graph, agents=AGENTS).messages)

    assert "nothing runs before the Task" in messages
    assert "nothing runs after the Aura Result" in messages


# ── the path itself ──────────────────────────────────────────────────────────


def test_a_solid_branch_is_refused_and_named_on_the_node_it_leaves() -> None:
    graph = _straight(_agent())
    task = graph.task_node
    extra = _agent("scout0000000")
    assert task is not None
    graph = graph.with_node(extra).with_connection(
        _edge(ConnectionKind.STEP, task.node_id, extra.node_id, 7)
    )

    verdict = validate_graph(graph, agents=AGENTS)

    assert verdict.runnable is False
    assert any("leads to 2" in issue.message for issue in verdict.for_node(task.node_id))


def test_a_solid_join_is_refused() -> None:
    graph = _straight(_agent())
    result = graph.result_node
    extra = _agent("scout0000000")
    assert result is not None
    graph = graph.with_node(extra).with_connection(
        _edge(ConnectionKind.STEP, extra.node_id, result.node_id, 7)
    )

    verdict = validate_graph(graph, agents=AGENTS)

    assert any("follows 2" in issue.message for issue in verdict.for_node(result.node_id))


def test_a_loop_in_the_steps_is_refused() -> None:
    graph = new_graph(AgentScope.PROJECT, "Loop")
    task = graph.task_node
    first = _agent()
    second = _agent("scout0000000")
    assert task is not None
    graph = (
        graph.with_node(first)
        .with_node(second)
        .with_connection(_edge(ConnectionKind.STEP, task.node_id, first.node_id, 0))
        .with_connection(_edge(ConnectionKind.STEP, first.node_id, second.node_id, 1))
        .with_connection(_edge(ConnectionKind.STEP, second.node_id, first.node_id, 2))
    )

    verdict = validate_graph(graph, agents=AGENTS)

    assert any("run in a loop" in message for message in verdict.messages)


def test_a_connection_to_a_node_that_is_not_there_is_refused() -> None:
    graph = _straight(_agent())
    task = graph.task_node
    assert task is not None
    dangling = _edge(ConnectionKind.STEP, task.node_id, "nsomewhereelse", 8)

    verdict = validate_graph(graph.with_connection(dangling), agents=AGENTS)

    assert any(
        "not on this canvas" in issue.message
        for issue in verdict.for_connection(dangling.connection_id)
    )


def test_a_disconnected_draft_node_is_marked_but_the_graph_is_untouched() -> None:
    graph = _straight(_agent())
    draft = _agent("scout0000000")
    with_draft = graph.with_node(draft)

    verdict = validate_graph(with_draft, agents=AGENTS)

    assert verdict.runnable is False
    assert any(
        "not connected to the workflow yet" in issue.message
        for issue in verdict.for_node(draft.node_id)
    )
    assert with_draft.node(draft.node_id) is not None


# ── sub-agents ───────────────────────────────────────────────────────────────


def test_a_helper_hanging_off_a_step_is_allowed() -> None:
    graph = _straight(_agent())
    step_node = graph.nodes_of_kind(WorkflowNodeKind.AGENT)[0]
    helper = _agent("scout0000000")
    graph = graph.with_node(helper).with_connection(
        _edge(ConnectionKind.SUB_AGENT, step_node.node_id, helper.node_id, 5)
    )

    verdict = validate_graph(graph, agents=AGENTS)

    assert verdict.runnable is True


def test_a_sub_agent_may_not_have_sub_agents_of_its_own() -> None:
    graph = _straight(_agent())
    step_node = graph.nodes_of_kind(WorkflowNodeKind.AGENT)[0]
    helper = _agent("scout0000000")
    deeper = _agent("scout0000000")
    nested = _edge(ConnectionKind.SUB_AGENT, helper.node_id, deeper.node_id, 6)
    graph = (
        graph.with_node(helper)
        .with_node(deeper)
        .with_connection(
            _edge(ConnectionKind.SUB_AGENT, step_node.node_id, helper.node_id, 5)
        )
        .with_connection(nested)
    )

    verdict = validate_graph(graph, agents=AGENTS)

    assert any(
        "one level deep" in issue.message
        for issue in verdict.for_connection(nested.connection_id)
    )


def test_an_agent_is_either_a_step_or_a_helper_but_not_both() -> None:
    graph = _straight(_agent(), _agent("scout0000000"))
    first, second = graph.nodes_of_kind(WorkflowNodeKind.AGENT)
    crossing = _edge(ConnectionKind.SUB_AGENT, first.node_id, second.node_id, 5)

    verdict = validate_graph(graph.with_connection(crossing), agents=AGENTS)

    assert any(
        "not both" in issue.message
        for issue in verdict.for_connection(crossing.connection_id)
    )


def test_the_fixed_ends_cannot_take_part_in_a_sub_agent_line() -> None:
    graph = _straight(_agent())
    task = graph.task_node
    helper = _agent("scout0000000")
    assert task is not None
    from_task = _edge(ConnectionKind.SUB_AGENT, task.node_id, helper.node_id, 5)

    verdict = validate_graph(
        graph.with_node(helper).with_connection(from_task), agents=AGENTS
    )

    assert any(
        "cannot have sub-agents" in issue.message
        for issue in verdict.for_connection(from_task.connection_id)
    )


# ── what the nodes point at ──────────────────────────────────────────────────


def test_a_missing_agent_is_named_on_its_own_node() -> None:
    ghost = _agent("goneagentid0")
    graph = _straight(ghost)

    verdict = validate_graph(graph, agents=AGENTS)

    assert verdict.runnable is False
    issues = verdict.for_node(ghost.node_id)
    assert issues and issues[0].message.startswith(MISSING_AGENT_LABEL)
    assert graph.node(ghost.node_id).agent_id == "goneagentid0"


def test_a_project_workflow_may_not_use_a_personal_agent() -> None:
    graph = _straight(_agent("personalagent"))

    verdict = validate_graph(graph, agents=AGENTS)

    assert any("cannot use a personal agent" in message for message in verdict.messages)


def test_a_personal_workflow_may_use_either_kind_of_agent() -> None:
    graph = _straight(_agent("personalagent"), _agent("reviewer0000"))
    personal = WorkflowGraph(
        graph_id=graph.graph_id,
        scope=AgentScope.PERSONAL,
        name=graph.name,
        nodes=graph.nodes,
        connections=graph.connections,
    )

    assert validate_graph(personal, agents=AGENTS).runnable is True


@pytest.mark.parametrize(
    ("graph_scope", "agent_scope", "refused"),
    [
        (AgentScope.PROJECT, AgentScope.PROJECT, False),
        (AgentScope.PROJECT, AgentScope.PERSONAL, True),
        (AgentScope.PERSONAL, AgentScope.PROJECT, False),
        (AgentScope.PERSONAL, AgentScope.PERSONAL, False),
    ],
)
def test_the_reference_rule_is_stated_once_for_every_surface(
    graph_scope: AgentScope, agent_scope: AgentScope, refused: bool
) -> None:
    assert bool(reference_scope_error(graph_scope, agent_scope)) is refused


# ── issues are addressable ───────────────────────────────────────────────────


def test_a_self_joining_connection_is_refused() -> None:
    graph = _straight(_agent())
    node = graph.nodes_of_kind(WorkflowNodeKind.AGENT)[0]
    loop = _edge(ConnectionKind.STEP, node.node_id, node.node_id, 9)

    verdict = validate_graph(graph.with_connection(loop), agents=AGENTS)

    assert any(
        "join a node to itself" in issue.message
        for issue in verdict.for_connection(loop.connection_id)
    )


def test_the_summary_counts_what_is_left_to_fix() -> None:
    graph = new_graph(AgentScope.PROJECT, "Draft").with_node(
        WorkflowNode(
            node_id=new_node_id(),
            kind=WorkflowNodeKind.AGENT,
            position=Point(),
            agent_id="goneagentid0",
        )
    )

    verdict = validate_graph(graph, agents=AGENTS)

    assert verdict.runnable is False
    assert verdict.summary.endswith("before this workflow could be run.")
