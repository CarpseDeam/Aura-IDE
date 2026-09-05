"""Root authoring authority and frozen execution remain independent."""

import threading
from dataclasses import asdict, replace

import pytest
from test_workflow_authoring import authoring_setup, editable_spec, review_spec

from aura.agents.turn_context import AgentTurnContext, AgentTurnMode
from aura.agents.workflow_plan import freeze_workflow_plan
from aura.agents.workflow_runner import WorkflowRunResult, WorkflowRunStatus
from aura.conversation.tools.registry import ToolRegistry


def test_root_tools_create_inspect_update_undo_with_agents_off(tmp_path):
    service, session = authoring_setup(tmp_path)
    registry = ToolRegistry(tmp_path / "project")
    events = []

    class Observer:
        def workflow_authored(self, saved):
            events.append(saved)

    registry.set_workflow_authoring(service, Observer())
    catalog = registry.tool_defs()

    def call(name, payload):
        result = registry.execute(name, payload, approval_cb=None)
        assert result.ok, result.payload
        return result.payload

    created = call("create_workflow", asdict(review_spec()))
    workflow_id = created["workflow_id"]
    assert created["executed"] is False
    inspected = call("inspect_workflow", {"workflow_id": workflow_id})
    updated = call("update_workflow", {**inspected, "name": "Careful review"})
    assert updated["workflow_id"] == workflow_id
    undone = call("undo_workflow_edit", updated)
    assert undone["name"] == created["name"]
    assert [event.status for event in events] == ["Saved", "Updated", "Undone"]
    assert registry.tool_defs() == catalog  # saves never mutate this turn's catalog
    assert registry.turn_agent_context.mode is AgentTurnMode.OFF
    assert not session.is_enabled()


@pytest.mark.parametrize("blocked", ["read_only", "plan", "cancel", "reject", "child", "missing"])
def test_authoring_cannot_bypass_turn_authority(tmp_path, blocked):
    service, _ = authoring_setup(tmp_path)
    registry = ToolRegistry(tmp_path / "project", read_only=blocked == "read_only", isolated_agent=blocked == "child")
    if blocked != "missing":
        registry.set_workflow_authoring(service)
    if blocked == "plan":
        registry.plan_review.begin_turn(required=True)
    cancel = threading.Event()
    if blocked == "cancel":
        cancel.set()
    result = registry.execute(
        "create_workflow", asdict(review_spec()), approval_cb=None, reject_all=blocked == "reject", cancel_event=cancel
    )
    assert not result.ok
    assert service.workflows.list_summaries() == ()
    assert service.agents.list_summaries() == ()
    if blocked in {"read_only", "plan"}:
        assert registry.execute("inspect_workflow", {"workflow_id": ""}, None).ok
    if blocked in {"child", "missing", "read_only"}:
        assert "create_workflow" not in {row["function"]["name"] for row in registry.tool_defs()}


def test_explicit_run_uses_exact_frozen_workflow_and_never_automatic_team(tmp_path, monkeypatch):
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
    registry = ToolRegistry(tmp_path / "project")
    registry.set_workflow_authoring(service)
    registry.set_agent_turn_context(
        AgentTurnContext.enabled(
            workflows=(plan,),
            explicit_workflow_id=plan.graph_id,
            root_provider="deepseek",
            root_model="deepseek-chat",
        )
    )
    calls = []

    class Runner:
        def run(self, frozen, task, **kwargs):
            calls.append((frozen, task))
            return WorkflowRunResult(
                status=WorkflowRunStatus.COMPLETED,
                graph_id=frozen.graph_id,
                workflow_name=frozen.name,
                result="Reviewed",
            )

    registry.set_agent_workflow_runner(Runner())
    names = {row["function"]["name"] for row in registry.tool_defs()}
    assert "run_agent_workflow" in names
    assert "run_agent_team" not in names
    assert not registry.execute("run_agent_team", {}, None).ok
    assert not registry.execute("run_agent_workflow", {"workflow_id": "other", "task": "Check"}, None).ok
    service.update(plan.graph_id, document.revision, replace(editable_spec(document), name="Later revision"))
    result = registry.execute("run_agent_workflow", {"workflow_id": plan.graph_id, "task": "Check reset"}, None)
    assert result.ok, result.payload
    assert calls == [(plan, "Check reset")]
    assert calls[0][0].name == document.graph.name
