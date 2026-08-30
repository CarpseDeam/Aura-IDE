"""Workflow graphs on disk: identity, references, duplicates, and round trips.

A workflow's identity is the opaque id minted at creation. It survives every
rename, it is what the file is named, and it is unique across both scopes — a
project and a personal workflow that claim the same id are both refused rather
than silently resolved in one direction.

A workflow also never carries authority. It cannot declare that Aura may run
it, and it cannot declare what anything it references is allowed to do: both
are private local state (see ``test_agents_workflow_state``), and a file that
tries to declare either is rejected outright, so a repository cannot ship a
workflow that appears to switch itself on.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.agents.graph_document import parse_graph_document, render_graph_document
from aura.agents.graph_models import (
    ConnectionKind,
    Point,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
    is_valid_graph_id,
    new_connection_id,
    new_graph_id,
    new_node_id,
)
from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError
from aura.agents.identity import AgentScope


@pytest.fixture()
def store(tmp_path: Path) -> AgentGraphStore:
    return AgentGraphStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")


def _scoped_store(tmp_path: Path, scopes: dict[str, AgentScope]) -> AgentGraphStore:
    """A store that knows which agents exist and where they live."""
    return AgentGraphStore(
        tmp_path / "workspace",
        personal_dir=tmp_path / "personal",
        agent_scopes=lambda: scopes,
    )


def _agent_node(agent_id: str, *, assignment: str = "", x: float = 0.0) -> WorkflowNode:
    return WorkflowNode(
        node_id=new_node_id(),
        kind=WorkflowNodeKind.AGENT,
        position=Point(x, 0.0),
        agent_id=agent_id,
        assignment=assignment,
    )


def _step(source: str, target: str, *, order: int = 0, bend: Point | None = None):
    return WorkflowConnection(
        connection_id=new_connection_id(),
        kind=ConnectionKind.STEP,
        source_id=source,
        target_id=target,
        order=order,
        bend=bend,
    )


# ── identity ─────────────────────────────────────────────────────────────────


def test_new_graph_ids_are_opaque_unique_and_file_safe() -> None:
    ids = {new_graph_id() for _ in range(50)}

    assert len(ids) == 50
    assert all(is_valid_graph_id(graph_id) for graph_id in ids)
    assert not any({"/", "\\", ":", "."} & set(graph_id) for graph_id in ids)


@pytest.mark.parametrize(
    "raw", ["", "no", "../escape", "has/slash", "has\\slash", "UPPER", ".hidden", "a" * 65]
)
def test_unsafe_graph_ids_are_refused(store: AgentGraphStore, raw: str) -> None:
    with pytest.raises(AgentGraphStoreError):
        store.path_for(AgentScope.PROJECT, raw)


def test_renaming_keeps_the_id_and_the_file(store: AgentGraphStore) -> None:
    graph = store.create(AgentScope.PROJECT, name="Draft")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)

    store.save(graph.with_name("Release review"))

    reloaded = store.get(graph.graph_id)
    assert reloaded is not None
    assert reloaded.graph_id == graph.graph_id
    assert reloaded.name == "Release review"
    assert store.path_for(AgentScope.PROJECT, graph.graph_id) == path


# ── where a workflow lives ───────────────────────────────────────────────────


def test_a_project_workflow_is_written_inside_the_project(
    store: AgentGraphStore, tmp_path: Path
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")

    path = tmp_path / "workspace" / ".aura" / "agents" / "workflows" / f"{graph.graph_id}.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == graph.graph_id


def test_a_personal_workflow_is_written_outside_the_project(
    store: AgentGraphStore, tmp_path: Path
) -> None:
    graph = store.create(AgentScope.PERSONAL, name="My review")

    assert (tmp_path / "personal" / f"{graph.graph_id}.json").is_file()
    assert not (tmp_path / "workspace" / ".aura" / "agents" / "workflows").exists()


def test_a_new_workflow_starts_with_only_its_two_fixed_ends(
    store: AgentGraphStore,
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")

    assert graph.task_node is not None
    assert graph.result_node is not None
    assert graph.nodes_of_kind(WorkflowNodeKind.AGENT) == ()
    assert graph.connections == ()


def test_duplicate_ids_across_scopes_refuse_both_sides(
    store: AgentGraphStore, tmp_path: Path
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    personal = tmp_path / "personal"
    personal.mkdir(parents=True, exist_ok=True)
    (personal / f"{graph.graph_id}.json").write_text(
        render_graph_document(graph), encoding="utf-8"
    )

    rows = store.list_summaries()

    assert len(rows) == 2
    assert not any(row.valid for row in rows)
    assert store.get(graph.graph_id) is None
    assert all("more than one workflow" in " ".join(row.errors) for row in rows)


def test_deleting_removes_exactly_one_scope(
    store: AgentGraphStore, tmp_path: Path
) -> None:
    project = store.create(AgentScope.PROJECT, name="Release review")
    personal = store.create(AgentScope.PERSONAL, name="My review")

    assert store.delete(AgentScope.PROJECT, project.graph_id) is True

    assert store.get(project.graph_id) is None
    assert store.get(personal.graph_id) is not None
    assert (tmp_path / "personal" / f"{personal.graph_id}.json").is_file()


# ── round trips ──────────────────────────────────────────────────────────────


def test_a_whole_workflow_survives_a_save_and_a_reload(
    store: AgentGraphStore,
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    reviewer = _agent_node("reviewer0000", assignment="Read the diff twice.", x=0.0)
    task = graph.task_node
    result = graph.result_node
    assert task is not None and result is not None
    graph = (
        graph.with_name("Release review", "What we do before tagging.")
        .with_node(reviewer)
        .with_connection(_step(task.node_id, reviewer.node_id, order=0))
        .with_connection(
            _step(reviewer.node_id, result.node_id, order=1, bend=Point(12.5, -40.0))
        )
    )

    store.save(graph)
    reloaded = store.get(graph.graph_id)

    assert reloaded == graph


def test_positions_and_manual_routing_are_persisted(store: AgentGraphStore) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    task = graph.task_node
    result = graph.result_node
    assert task is not None and result is not None
    graph = graph.with_node(task.moved_to(-17.5, 42.0)).with_connection(
        _step(task.node_id, result.node_id, bend=Point(-8.0, 30.0))
    )

    store.save(graph)
    reloaded = store.get(graph.graph_id)

    assert reloaded is not None
    assert reloaded.node(task.node_id).position == Point(-17.5, 42.0)
    assert reloaded.connections[0].bend == Point(-8.0, 30.0)


def test_a_node_references_an_agent_and_copies_nothing_from_it(
    store: AgentGraphStore,
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    graph = graph.with_node(_agent_node("reviewer0000", assignment="Read the diff."))

    store.save(graph)
    text = store.path_for(AgentScope.PROJECT, graph.graph_id).read_text(encoding="utf-8")

    assert "reviewer0000" in text
    for copied in ("instructions", "provider", "model", "thinking"):
        assert copied not in text.lower()


def test_the_same_agent_twice_is_two_occurrences_with_their_own_assignments(
    store: AgentGraphStore,
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    first = _agent_node("reviewer0000", assignment="Read the diff.", x=-60.0)
    second = _agent_node("reviewer0000", assignment="Read it again.", x=60.0)

    store.save(graph.with_node(first).with_node(second))
    reloaded = store.get(graph.graph_id)

    assert reloaded is not None
    assert first.node_id != second.node_id
    assert reloaded.agent_ids == ("reviewer0000",)
    assert reloaded.node(first.node_id).assignment == "Read the diff."
    assert reloaded.node(second.node_id).assignment == "Read it again."


def test_removing_an_occurrence_leaves_the_other_one_alone(
    store: AgentGraphStore,
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    first = _agent_node("reviewer0000", assignment="Read the diff.")
    second = _agent_node("reviewer0000", assignment="Read it again.")
    graph = graph.with_node(first).with_node(second)

    store.save(graph.without_node(first.node_id))
    reloaded = store.get(graph.graph_id)

    assert reloaded is not None
    assert reloaded.node(first.node_id) is None
    assert reloaded.node(second.node_id).assignment == "Read it again."


def test_a_missing_agent_reference_is_kept_rather_than_dropped(
    tmp_path: Path,
) -> None:
    store = _scoped_store(tmp_path, {})
    graph = store.create(AgentScope.PROJECT, name="Release review")
    ghost = _agent_node("goneagentid0")

    store.save(graph.with_node(ghost))
    reloaded = store.get(graph.graph_id)

    assert reloaded is not None
    assert reloaded.node(ghost.node_id).agent_id == "goneagentid0"


# ── what a workflow file may never say ───────────────────────────────────────


@pytest.mark.parametrize("key", ["available", "enabled", "permissions", "authority"])
def test_a_workflow_cannot_declare_that_it_is_switched_on(
    store: AgentGraphStore, tmp_path: Path, key: str
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)
    document = json.loads(path.read_text(encoding="utf-8"))
    document[key] = True
    path.write_text(json.dumps(document), encoding="utf-8")

    row = store.summary(graph.graph_id)

    assert row is not None
    assert row.valid is False
    assert store.get(graph.graph_id) is None
    assert any(key in message for message in row.errors)


def test_a_declared_id_must_match_the_file_it_is_in(
    store: AgentGraphStore, tmp_path: Path
) -> None:
    directory = tmp_path / "workspace" / ".aura" / "agents" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    graph = WorkflowGraph(
        graph_id="claimedidxxxx", scope=AgentScope.PROJECT, name="Impostor"
    )
    (directory / "actualidxxxxx.json").write_text(
        render_graph_document(graph), encoding="utf-8"
    )

    row = store.summary("actualidxxxxx")

    assert row is not None
    assert row.valid is False
    assert any("does not match the file name" in message for message in row.errors)


def test_a_workflow_that_cannot_be_read_stays_a_visible_row(
    store: AgentGraphStore, tmp_path: Path
) -> None:
    directory = tmp_path / "workspace" / ".aura" / "agents" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brokenidxxxxx.json").write_text("{not json", encoding="utf-8")

    rows = store.list_summaries()

    assert [row.graph_id for row in rows] == ["brokenidxxxxx"]
    assert rows[0].valid is False


# ── which agents a workflow may point at ─────────────────────────────────────


def test_a_project_workflow_cannot_be_saved_with_a_personal_agent(
    tmp_path: Path,
) -> None:
    store = _scoped_store(tmp_path, {"personalagent": AgentScope.PERSONAL})
    graph = store.create(AgentScope.PROJECT, name="Release review")

    with pytest.raises(AgentGraphStoreError) as failure:
        store.save(graph.with_node(_agent_node("personalagent")))

    assert "personal agent" in str(failure.value)


def test_a_personal_workflow_may_point_at_either_kind_of_agent(
    tmp_path: Path,
) -> None:
    store = _scoped_store(
        tmp_path,
        {"projectagent": AgentScope.PROJECT, "personalagent": AgentScope.PERSONAL},
    )
    graph = store.create(AgentScope.PERSONAL, name="My review")

    saved = store.save(
        graph.with_node(_agent_node("projectagent")).with_node(
            _agent_node("personalagent")
        )
    )

    assert set(saved.agent_ids) == {"projectagent", "personalagent"}
    assert store.get(graph.graph_id) == saved


def test_a_project_workflow_may_point_at_a_project_agent(tmp_path: Path) -> None:
    store = _scoped_store(tmp_path, {"projectagent": AgentScope.PROJECT})
    graph = store.create(AgentScope.PROJECT, name="Release review")

    saved = store.save(graph.with_node(_agent_node("projectagent")))

    assert saved.agent_ids == ("projectagent",)


# ── the document itself ──────────────────────────────────────────────────────


def test_rendering_the_same_workflow_twice_produces_the_same_bytes(
    store: AgentGraphStore,
) -> None:
    graph = store.create(AgentScope.PROJECT, name="Release review")
    graph = graph.with_node(_agent_node("reviewer0000", assignment="Read it."))

    first = render_graph_document(graph)
    second = render_graph_document(graph)

    assert first == second
    parsed = parse_graph_document(
        first, scope=AgentScope.PROJECT, expected_id=graph.graph_id
    )
    assert parsed.graph == graph
