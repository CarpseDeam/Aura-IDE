"""Create, revise, undo and reload native Workflows without a model or Qt."""

from dataclasses import replace

import pytest

from aura.agents.graph_local_state import WorkflowLocalState
from aura.agents.graph_models import Point
from aura.agents.graph_session import WorkflowSession
from aura.agents.graph_store import AgentGraphStore, AgentGraphStoreError
from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentLocalState, AgentPermission
from aura.agents.store import AgentStore
from aura.agents.team_spec import (
    HandoffSpec,
    HelperSpec,
    NewAgentSpec,
    OccurrenceSpec,
    WorkflowSpec,
    parse_workflow_spec,
)
from aura.agents.turn_context import AgentModelTargets
from aura.agents.workflow_authoring import WorkflowAuthoring


def authoring_setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    agents = AgentStore(root, personal_dir=tmp_path / "agents")
    local = AgentLocalState(root, state_root=tmp_path / "state")
    workflows = AgentGraphStore(
        root,
        personal_dir=tmp_path / "workflows",
        agent_scopes=lambda: {row.agent_id: row.scope for row in agents.list_summaries() if row.valid},
    )
    session = WorkflowSession(
        root,
        store_factory=lambda _: workflows,
        state_factory=lambda _: WorkflowLocalState(root, state_root=tmp_path / "state"),
    )
    service = WorkflowAuthoring(
        agents=agents,
        workflows=workflows,
        local_state=local,
        edits=session.edits,
        model_targets=AgentModelTargets(),
    )
    return service, session


def review_spec(name="Change review"):
    return WorkflowSpec(
        name,
        "Review any requested change.",
        new_agents=(NewAgentSpec("reviewer", "Reviewer", "Reviews changes.", "Review the supplied task."),),
        occurrences=(OccurrenceSpec("review", "reviewer", "Review the requested change."),),
        handoffs=(HandoffSpec("task", "review"), HandoffSpec("review", "result")),
    )


def editable_spec(document):
    parsed = parse_workflow_spec(document.payload())
    assert parsed.ok, parsed.errors
    return parsed.spec


def test_authoring_saves_without_execution_configuration_and_reload_works(tmp_path):
    service, session = authoring_setup(tmp_path)
    saved = service.create(review_spec())
    graph = saved.document.graph
    assert graph.scope is AgentScope.PERSONAL
    assert saved.status == "Saved"
    assert service.create(review_spec()).document == saved.document  # retry keeps exact identities
    assert len(service.agents.list_summaries()) == 1
    assert not session.is_enabled()
    assert all(entry.definition.model == "" for entry in saved.document.agents)
    # A fresh service/session needs only the standard on-disk formats.
    fresh, _ = authoring_setup(tmp_path)
    assert fresh.document(graph.graph_id) == saved.document
    assert fresh.inspect()["workflows"][0]["workflow_id"] == graph.graph_id


def test_canvas_and_chat_share_identity_layout_and_undo(tmp_path):
    service, session = authoring_setup(tmp_path)
    initial = service.create(review_spec()).document
    session.open(initial.graph.graph_id)
    node = next(node for node in session.graph.nodes if node.is_agent)
    moved = session.graph.with_node(node.moved_to(777, 222))
    session.commit(moved)
    inspected = service.document(initial.graph.graph_id)
    spec = replace(editable_spec(inspected), name="Stricter review")
    updated = service.update(initial.graph.graph_id, inspected.revision, spec)
    assert updated.document.graph.node(node.node_id).position == Point(777, 222)
    assert updated.document.graph.connections == initial.graph.connections
    assert session.can_undo  # stale canvas presentation must not reset chat history
    session.reload()
    assert session.can_undo
    assert session.undo() == moved  # undo chat edit from canvas
    current = service.document(initial.graph.graph_id)
    undone = service.undo(initial.graph.graph_id, current.revision)
    assert undone.document.graph == initial.graph  # undo canvas edit from chat
    assert session.graph_id == initial.graph.graph_id


@pytest.mark.parametrize("change", ["graph", "definition", "permission"])
def test_stale_chat_edit_and_undo_do_not_overwrite_newer_changes(tmp_path, change):
    service, session = authoring_setup(tmp_path)
    original = service.create(review_spec()).document
    entry = original.agents[0]
    if change == "graph":
        service.workflows.save(original.graph.with_name("External rename"))
    elif change == "definition":
        service.agents.update(replace(entry.definition, instructions="New instructions"))
    else:
        service.local_state.set_permission(entry.agent_id, AgentPermission.READ_WRITE)
    latest = service.document(original.graph.graph_id)
    for action in [
        lambda: service.update(original.graph.graph_id, original.revision, editable_spec(original)),
        lambda: service.undo(original.graph.graph_id, original.revision),
    ]:
        with pytest.raises(AgentGraphStoreError, match="changed"):
            action()
        assert service.document(original.graph.graph_id) == latest


def test_reused_agents_and_complete_topology_survive_revision(tmp_path):
    service, _ = authoring_setup(tmp_path)
    initial = service.create(review_spec()).document
    agent = initial.agents[0].definition
    spec = WorkflowSpec(
        "Review branches",
        new_agents=(),
        occurrences=tuple(
            OccurrenceSpec(alias, agent.agent_id, f"Perform the {alias} role for this task.")
            for alias in ("left", "right", "join", "helper", "nested")
        ),
        handoffs=(
            HandoffSpec("task", "left"),
            HandoffSpec("task", "right"),
            HandoffSpec("left", "join"),
            HandoffSpec("right", "join"),
            HandoffSpec("join", "result"),
        ),
        helpers=(HelperSpec("join", "helper"), HelperSpec("helper", "nested")),
    )
    saved = service.create(spec).document
    update = replace(editable_spec(saved), description="Updated description")
    result = service.update(saved.graph.graph_id, saved.revision, update).document
    assert result.graph.nodes == saved.graph.nodes
    assert result.graph.connections == saved.graph.connections
    assert service.agents.get(agent.agent_id) == agent
    assert service.document(initial.graph.graph_id) == initial
    assert len(service.agents.list_summaries()) == 1


def test_invalid_update_preserves_saved_workflow_and_agents(tmp_path):
    service, _ = authoring_setup(tmp_path)
    initial = service.create(review_spec()).document
    broken = replace(editable_spec(initial), handoffs=())
    with pytest.raises(AgentGraphStoreError):
        service.update(initial.graph.graph_id, initial.revision, broken)
    assert service.document(initial.graph.graph_id) == initial
    assert len(service.agents.list_summaries()) == 1


def test_failed_save_does_not_consume_undo(tmp_path, monkeypatch):
    service, session = authoring_setup(tmp_path)
    original = service.create(review_spec()).document
    session.open(original.graph.graph_id)
    session.rename("Renamed")

    def fail(graph):
        raise AgentGraphStoreError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(service.workflows, "save", fail)
        with pytest.raises(AgentGraphStoreError, match="disk full"):
            session.undo()
    assert session.can_undo
    assert session.undo() == original.graph


def test_create_retry_after_partial_save_reuses_generated_definitions(tmp_path, monkeypatch):
    service, _ = authoring_setup(tmp_path)

    def fail(graph):
        raise AgentGraphStoreError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(service.workflows, "create_supplied", fail)
        with pytest.raises(Exception, match="disk full"):
            service.create(review_spec())
    ids = [row.agent_id for row in service.agents.list_summaries()]
    saved = service.create(review_spec())
    assert [entry.agent_id for entry in saved.document.agents] == ids
    assert len(service.agents.list_summaries()) == 1


def test_saved_process_accepts_two_independent_run_tasks(tmp_path, monkeypatch):
    from aura.agents.delegation import DelegationResult, DelegationStatus
    from aura.agents.workflow_plan import freeze_workflow_plan
    from aura.agents.workflow_runner import WorkflowRunner

    monkeypatch.setattr("aura.config.has_usable_provider_configuration", lambda _: True)
    service, _ = authoring_setup(tmp_path)
    document = service.create(review_spec()).document
    plan, errors = freeze_workflow_plan(
        document.graph,
        definitions=service.agents,
        permissions=service.local_state,
        agent_scopes={entry.agent_id: entry.definition.scope for entry in document.agents},
        provider="deepseek",
        model="deepseek-chat",
        thinking="off",
    )
    assert not errors
    seen = []

    class Child:
        def run(self, entry, task, *args, **kwargs):
            seen.append(task)
            return DelegationResult(agent_id=entry.agent_id, status=DelegationStatus.COMPLETED, result="Reviewed"), ()

    runner = WorkflowRunner(workspace_root=tmp_path / "project", child=Child())
    for task in ("Review password reset", "Review invoice export"):
        result = runner.run(plan, task)
        assert result.ok, result.error
        assert task in seen[-1]
    assert "password reset" not in seen[-1]
    assert service.document(document.graph.graph_id) == document


def test_update_retry_after_partial_save_keeps_new_agent_identity(tmp_path, monkeypatch):
    service, _ = authoring_setup(tmp_path)
    original = service.create(review_spec()).document
    spec = replace(review_spec(), name="Specialist review")

    def fail(graph):
        raise AgentGraphStoreError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(service.workflows, "save", fail)
        with pytest.raises(Exception, match="disk full"):
            service.update(original.graph.graph_id, original.revision, spec)
    ids = {row.agent_id for row in service.agents.list_summaries()}
    saved = service.update(original.graph.graph_id, original.revision, spec)
    assert {row.agent_id for row in service.agents.list_summaries()} == ids
    assert saved.document.graph.graph_id == original.graph.graph_id
