"""What shape a workflow's solid lines make, and in what order it is read.

The DAG is the authority the plan freezes and the runner walks, so what
matters here is not that a shape is *accepted* — that is
``test_agents_graph_validation`` — but that the same drawing always comes back
as the same steps, in the same order, waiting for the same predecessors.
"""
from __future__ import annotations

from aura.agents.graph_dag import runnable_dag, solid_dag
from aura.agents.graph_models import (
    ConnectionKind,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
)
from aura.agents.identity import AgentScope


def _node(node_id: str) -> WorkflowNode:
    return WorkflowNode(node_id, WorkflowNodeKind.AGENT, agent_id="reviewer0000")


def _graph(*, nodes: tuple[str, ...], edges: tuple[tuple[str, str], ...]) -> WorkflowGraph:
    """Task and Aura Result, plus the agent occurrences and lines named."""
    return WorkflowGraph(
        graph_id="dagworkflow01",
        scope=AgentScope.PROJECT,
        name="Shape",
        nodes=(
            WorkflowNode("task", WorkflowNodeKind.TASK),
            *(_node(node_id) for node_id in nodes),
            WorkflowNode("result", WorkflowNodeKind.AURA_RESULT),
        ),
        connections=tuple(
            WorkflowConnection(
                f"edge{index}", ConnectionKind.STEP, source, target, index
            )
            for index, (source, target) in enumerate(edges)
        ),
    )


def test_a_straight_line_reads_as_the_line_it_is() -> None:
    dag = solid_dag(
        _graph(
            nodes=("a", "b"),
            edges=(("task", "a"), ("a", "b"), ("b", "result")),
        )
    )

    assert dag is not None
    assert dag.node_ids == ("a", "b")
    assert dag.branched is False
    assert [step.predecessors for step in dag.steps] == [(), ("a",)]
    assert [step.successors for step in dag.steps] == [("b",), ()]
    assert dag.entry_node_ids == ("a",)
    assert dag.terminal_node_ids == ("b",)


def test_a_line_drawn_back_to_front_still_reads_in_dependency_order() -> None:
    """The file lists ``b`` before ``a``; the run still starts at ``a``."""
    dag = solid_dag(
        _graph(
            nodes=("b", "a"),
            edges=(("task", "a"), ("a", "b"), ("b", "result")),
        )
    )

    assert dag is not None
    assert dag.node_ids == ("a", "b")


def test_a_join_waits_for_its_branches_in_the_workflows_own_order() -> None:
    dag = solid_dag(
        _graph(
            nodes=("left", "right", "join"),
            edges=(
                ("task", "left"),
                ("task", "right"),
                ("right", "join"),
                ("left", "join"),
                ("join", "result"),
            ),
        )
    )

    assert dag is not None
    assert dag.branched is True
    assert dag.node_ids == ("left", "right", "join")
    join = dag.step("join")
    assert join is not None
    # Drawn right-first, so that is the order the join is handed them in —
    # never the order they happen to finish in.
    assert join.predecessors == ("right", "left")
    assert join.is_join is True
    assert dag.entry_node_ids == ("left", "right")
    assert dag.terminal_node_ids == ("join",)


def test_the_same_hand_off_drawn_twice_is_still_one_predecessor() -> None:
    dag = solid_dag(
        _graph(
            nodes=("a", "b"),
            edges=(("task", "a"), ("a", "b"), ("a", "b"), ("b", "result")),
        )
    )

    assert dag is not None
    assert dag.step("b").predecessors == ("a",)


def test_two_branches_may_each_speak_to_the_aura_result() -> None:
    dag = solid_dag(
        _graph(
            nodes=("first", "second"),
            edges=(
                ("task", "first"),
                ("task", "second"),
                ("first", "result"),
                ("second", "result"),
            ),
        )
    )

    assert dag is not None
    assert dag.terminal_node_ids == ("first", "second")
    assert dag.branched is True


def test_a_cycle_has_no_shape_to_read() -> None:
    assert (
        solid_dag(
            _graph(
                nodes=("a", "b"),
                edges=(("task", "a"), ("a", "b"), ("b", "a"), ("b", "result")),
            )
        )
        is None
    )


def test_a_branch_that_leads_nowhere_has_no_shape_to_read() -> None:
    assert (
        solid_dag(
            _graph(
                nodes=("a", "stray"),
                edges=(("task", "a"), ("a", "result"), ("task", "stray")),
            )
        )
        is None
    )


def test_nothing_may_run_before_the_task_or_after_the_result() -> None:
    assert (
        solid_dag(
            _graph(nodes=("a",), edges=(("a", "task"), ("task", "a"), ("a", "result")))
        )
        is None
    )
    assert (
        solid_dag(
            _graph(
                nodes=("a",),
                edges=(("task", "a"), ("a", "result"), ("result", "a")),
            )
        )
        is None
    )


def test_a_task_wired_straight_to_the_result_is_a_shape_with_nothing_to_run() -> None:
    graph = _graph(nodes=(), edges=(("task", "result"),))

    assert solid_dag(graph) is not None
    assert runnable_dag(graph) is None


def test_a_direct_result_connection_cannot_bypass_solid_agent_steps() -> None:
    graph = _graph(
        nodes=("agent",),
        edges=(("task", "agent"), ("agent", "result"), ("task", "result")),
    )

    assert solid_dag(graph) is None
    assert runnable_dag(graph) is None
