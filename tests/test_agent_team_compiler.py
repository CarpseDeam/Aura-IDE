"""Semantic automatic teams compile into Aura's one native workflow path."""
from __future__ import annotations

from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentDefinition, AgentThinking
from aura.agents.roster import EMPTY_AGENT_ROSTER, AgentRosterEntry, AgentTurnRoster
from aura.agents.team_compiler import compile_agent_team
from aura.agents.team_spec import (
    AgentTeamSpec,
    HandoffSpec,
    HelperSpec,
    NewAgentSpec,
    OccurrenceSpec,
    parse_agent_team_spec,
)
from aura.agents.turn_context import AgentModelTarget, AgentModelTargets


def _targets() -> AgentModelTargets:
    return AgentModelTargets.freeze(
        (
            AgentModelTarget(
                key="local-fast",
                provider="local_openai",
                model="qwen-coder",
                label="Local fast",
            ),
            AgentModelTarget(
                key="hosted-strong",
                provider="openai",
                model="gpt-5.5",
                label="Hosted strong",
            ),
            AgentModelTarget(
                key="hosted-helper",
                provider="deepseek",
                model="deepseek-v4-flash",
                label="Hosted helper",
            ),
        )
    )


def _new(
    alias: str,
    *,
    target: str = "inherit",
    thinking: str = "inherit",
    permission: str = "read_only",
) -> NewAgentSpec:
    return NewAgentSpec(
        alias=alias,
        name=alias.replace("_", " ").title(),
        description=f"Performs the {alias} specialist role.",
        instructions=f"Work carefully as the {alias} specialist.",
        model_target=target,
        thinking=thinking,
        permission=permission,
    )


def _compile(spec: AgentTeamSpec, monkeypatch, *, roster=EMPTY_AGENT_ROSTER):
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda _provider: True
    )
    return compile_agent_team(
        spec,
        roster=roster,
        model_targets=_targets(),
        provider="deepseek",
        model="deepseek-v4-pro",
        thinking="high",
    )


def test_parser_translates_flat_tool_names_and_defaults() -> None:
    parsed = parse_agent_team_spec(
        {
            "task": "  Review the change.  ",
            "team_name": "  Review team  ",
            "team_description": "  One careful pass.  ",
            "new_agents": [
                {
                    "alias": " reviewer ",
                    "name": " Reviewer ",
                    "description": " Reviews changes. ",
                    "instructions": " Inspect the complete change. ",
                }
            ],
            "occurrences": [
                {
                    "alias": " review ",
                    "agent_ref": " reviewer ",
                    "assignment": " Inspect it. ",
                }
            ],
            "handoffs": [
                {"source": " task ", "target": " review "},
                {"source": " review ", "target": " result "},
            ],
            "helpers": [],
        }
    )

    assert parsed.ok is True
    assert parsed.errors == ()
    assert parsed.spec is not None
    assert parsed.spec.task == "Review the change."
    assert parsed.spec.name == "Review team"
    assert parsed.spec.description == "One careful pass."
    assert parsed.spec.new_agents[0].model_target == "inherit"
    assert parsed.spec.new_agents[0].thinking == "inherit"
    assert parsed.spec.new_agents[0].permission == "read_only"
    assert parsed.spec.handoffs[-1] == HandoffSpec("review", "result")


def test_parser_refuses_non_text_and_non_object_rows() -> None:
    parsed = parse_agent_team_spec(
        {
            "task": 42,
            "team_name": "Team",
            "new_agents": ["not an object"],
            "occurrences": {},
            "handoffs": [],
            "helpers": [],
        }
    )

    assert parsed.spec is None
    assert "task must be text" in parsed.errors
    assert "new_agents[0] is not an object" in parsed.errors
    assert "occurrences must be an array" in parsed.errors


def test_full_six_occurrence_topology_uses_the_native_frozen_plan(
    monkeypatch,
) -> None:
    spec = AgentTeamSpec(
        task="Investigate two paths, implement the fix, and review it.",
        name="Repair team",
        description="Parallel investigation, one join, and nested expert help.",
        new_agents=(
            _new("investigator", target="local-fast", thinking="max"),
            _new(
                "implementer",
                target="hosted-strong",
                thinking="max",
                permission="read_write",
            ),
            _new("reviewer", permission="read_write"),
            _new("api_helper", target="hosted-helper"),
            _new("nested_helper", target="local-fast", thinking="high"),
        ),
        occurrences=(
            OccurrenceSpec("left", "investigator", "Inspect the data path."),
            OccurrenceSpec("right", "investigator", "Inspect the API path."),
            OccurrenceSpec("build", "implementer", "Implement from both reports."),
            OccurrenceSpec("review", "reviewer", "Review the completed change."),
            OccurrenceSpec("api_help", "api_helper", "Answer focused API questions."),
            OccurrenceSpec("deep_help", "nested_helper", "Verify the helper answer."),
        ),
        handoffs=(
            HandoffSpec("task", "left"),
            HandoffSpec("task", "right"),
            # This input order is the join bundle's frozen order.
            HandoffSpec("right", "build"),
            HandoffSpec("left", "build"),
            HandoffSpec("build", "review"),
            HandoffSpec("review", "result"),
        ),
        helpers=(
            HelperSpec("build", "api_help"),
            HelperSpec("api_help", "deep_help"),
        ),
    )

    compiled, errors = _compile(spec, monkeypatch)

    assert errors == ()
    assert compiled is not None
    assert compiled.task == spec.task
    assert compiled.plan.graph.scope is AgentScope.PERSONAL
    assert len(compiled.generated_definitions) == 5

    left, right, build, review = compiled.plan.steps
    assert [step.assignment for step in compiled.plan.steps] == [
        "Inspect the data path.",
        "Inspect the API path.",
        "Implement from both reports.",
        "Review the completed change.",
    ]
    assert left.agent_id == right.agent_id
    assert build.predecessors == (right.node_id, left.node_id)
    assert build.permission is AgentPermission.READ_WRITE
    assert review.permission is AgentPermission.READ_WRITE
    assert compiled.plan.writable is True

    assert (left.resolved.provider, left.resolved.model, left.resolved.thinking) == (
        "local_openai",
        "qwen-coder",
        "off",
    )
    assert (build.resolved.provider, build.resolved.model) == (
        "openai",
        "gpt-5.5",
    )
    assert (review.resolved.provider, review.resolved.model) == (
        "deepseek",
        "deepseek-v4-pro",
    )

    api_helper = build.helpers[0]
    nested_helper = api_helper.children[0]
    assert api_helper.immediate_parent_node_id == build.node_id
    assert nested_helper.immediate_parent_node_id == api_helper.node_id
    assert nested_helper.depth == 2
    assert (nested_helper.resolved.provider, nested_helper.resolved.model) == (
        "local_openai",
        "qwen-coder",
    )

    graph = compiled.plan.graph
    task = graph.task_node
    result = graph.result_node
    assert task is not None and result is not None
    left_node = graph.node(left.node_id)
    right_node = graph.node(right.node_id)
    build_node = graph.node(build.node_id)
    review_node = graph.node(review.node_id)
    helper_node = graph.node(api_helper.node_id)
    nested_node = graph.node(nested_helper.node_id)
    assert all(
        node is not None
        for node in (
            left_node,
            right_node,
            build_node,
            review_node,
            helper_node,
            nested_node,
        )
    )
    assert task.position.x < left_node.position.x  # type: ignore[union-attr]
    assert left_node.position.x == right_node.position.x  # type: ignore[union-attr]
    assert left_node.position.y != right_node.position.y  # type: ignore[union-attr]
    assert right_node.position.x < build_node.position.x  # type: ignore[union-attr]
    assert build_node.position.x < review_node.position.x  # type: ignore[union-attr]
    assert review_node.position.x < result.position.x  # type: ignore[union-attr]
    solid_bottom = max(
        left_node.position.y,  # type: ignore[union-attr]
        right_node.position.y,  # type: ignore[union-attr]
        build_node.position.y,  # type: ignore[union-attr]
        review_node.position.y,  # type: ignore[union-attr]
    )
    assert helper_node.position.y > solid_bottom  # type: ignore[union-attr]
    assert nested_node.position.y > helper_node.position.y  # type: ignore[union-attr]

    assert [edge.order for edge in graph.connections] == list(
        range(len(graph.connections))
    )


def test_existing_agent_keeps_its_frozen_definition_permission_and_target(
    monkeypatch,
) -> None:
    definition = AgentDefinition(
        agent_id="existingreviewer01",
        scope=AgentScope.PROJECT,
        name="Existing reviewer",
        description="Reviews the requested implementation.",
        instructions="Review the code and report concrete defects.",
        provider="openai",
        model="gpt-5.5",
        thinking=AgentThinking.MAX,
    )
    roster = AgentTurnRoster(
        (
            AgentRosterEntry(
                definition=definition,
                permission=AgentPermission.READ_WRITE,
            ),
        )
    )
    spec = AgentTeamSpec(
        task="Review this implementation.",
        name="Existing review team",
        occurrences=(
            OccurrenceSpec(
                "review", definition.agent_id, "Inspect every relevant change."
            ),
        ),
        handoffs=(
            HandoffSpec("task", "review"),
            HandoffSpec("review", "result"),
        ),
    )

    compiled, errors = _compile(spec, monkeypatch, roster=roster)

    assert errors == ()
    assert compiled is not None
    assert compiled.generated_definitions == ()
    step = compiled.plan.steps[0]
    assert step.definition is definition
    assert step.permission is AgentPermission.READ_WRITE
    assert (step.resolved.provider, step.resolved.model, step.resolved.thinking) == (
        "openai",
        "gpt-5.5",
        "max",
    )


def test_limit_counts_every_occurrence_including_helpers(monkeypatch) -> None:
    definition = AgentDefinition(
        agent_id="existingreader001",
        scope=AgentScope.PERSONAL,
        name="Reader",
        description="Reads the requested material.",
        instructions="Read carefully.",
    )
    roster = AgentTurnRoster((AgentRosterEntry(definition),))
    occurrences = tuple(
        OccurrenceSpec(f"place{index}", definition.agent_id, f"Assignment {index}")
        for index in range(7)
    )
    spec = AgentTeamSpec(
        task="Do seven pieces of work.",
        name="Oversized team",
        occurrences=occurrences,
        handoffs=(HandoffSpec("task", "place0"),),
        helpers=tuple(
            HelperSpec(f"place{index}", f"place{index + 1}")
            for index in range(6)
        ),
    )

    compiled, errors = _compile(spec, monkeypatch, roster=roster)

    assert compiled is None
    assert any("including helpers" in error and "has 7" in error for error in errors)


def test_unknown_model_duplicate_edges_and_unused_agents_are_refused(
    monkeypatch,
) -> None:
    spec = AgentTeamSpec(
        task="Inspect the code.",
        name="Invalid team",
        new_agents=(
            _new("reader", target="invented-model"),
            _new("unused"),
        ),
        occurrences=(OccurrenceSpec("read", "reader", "Inspect it."),),
        handoffs=(
            HandoffSpec("task", "read"),
            HandoffSpec("task", "read"),
            HandoffSpec("read", "result"),
        ),
    )

    compiled, errors = _compile(spec, monkeypatch)

    assert compiled is None
    assert any("invented-model" in error and "not available" in error for error in errors)
    assert "new Agent 'unused' is never used by an occurrence" in errors
    assert any("duplicates the handoff task -> read" in error for error in errors)


def test_native_cycle_errors_are_reported_with_semantic_occurrence_aliases(
    monkeypatch,
) -> None:
    spec = AgentTeamSpec(
        task="Attempt a cyclic workflow.",
        name="Cyclic team",
        new_agents=(_new("worker"),),
        occurrences=(
            OccurrenceSpec("first", "worker", "Do the first pass."),
            OccurrenceSpec("second", "worker", "Do the second pass."),
        ),
        handoffs=(
            HandoffSpec("task", "first"),
            HandoffSpec("first", "second"),
            HandoffSpec("second", "first"),
            HandoffSpec("second", "result"),
        ),
    )

    compiled, errors = _compile(spec, monkeypatch)

    assert compiled is None
    assert any(error.startswith("occurrence 'first':") for error in errors)
    assert any(error.startswith("occurrence 'second':") for error in errors)
    assert {error.split(":", 1)[0] for error in errors} == {
        "occurrence 'first'",
        "occurrence 'second'",
    }
