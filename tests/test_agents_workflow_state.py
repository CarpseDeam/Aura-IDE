"""Private workflow state keeps only editor selection and the Agents gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.agents.graph_local_state import (
    ENABLED_NOTE,
    WorkflowLocalState,
    WorkflowLocalStateError,
)
from aura.agents.graph_session import WorkflowSession
from aura.agents.graph_store import AgentGraphStore
from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentLocalState

FIRST = "workflowone1"
SECOND = "workflowtwo2"


@pytest.fixture()
def state(tmp_path: Path) -> WorkflowLocalState:
    return WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")


def test_defaults_and_gate_are_independent_of_editor_selection(
    state: WorkflowLocalState,
) -> None:
    assert state.selected_id() == ""
    assert state.is_enabled() is False

    state.set_enabled(True)
    state.set_selected(FIRST)
    state.set_selected(SECOND)
    state.set_selected("")

    assert state.selected_id() == ""
    assert state.is_enabled() is True
    state.set_enabled(False)
    assert state.is_enabled() is False


def test_the_switch_describes_the_complete_private_capability() -> None:
    assert "private" in ENABLED_NOTE
    assert "any saved runnable Workflow" in ENABLED_NOTE
    assert "Workflow selection affects only editing" in ENABLED_NOTE


def test_forgetting_selection_does_not_disable_agents(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_enabled(True)

    state.forget(FIRST)

    assert state.selected_id() == ""
    assert state.is_enabled() is True


def test_forgetting_an_unrelated_workflow_changes_nothing(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_enabled(True)
    state.forget(SECOND)
    assert state.selected_id() == FIRST
    assert state.is_enabled() is True


@pytest.mark.parametrize("raw", ["no", "../escape", "has/slash", "UPPER"])
def test_an_unusable_editor_workflow_id_is_refused(
    state: WorkflowLocalState, raw: str
) -> None:
    with pytest.raises(WorkflowLocalStateError):
        state.set_selected(raw)


@pytest.mark.parametrize(
    ("document", "enabled"),
    [
        ({"version": 1, "available": [FIRST, SECOND], "selected": FIRST}, True),
        ({"version": 1, "available": [SECOND], "selected": FIRST}, False),
        ({"version": 2, "selected": FIRST, "enabled": True}, True),
        ({"version": 2, "selected": FIRST, "enabled": False}, False),
        (
            {
                "version": 3,
                "selected": FIRST,
                "active_workflow": SECOND,
                "enabled": True,
            },
            True,
        ),
        (
            {
                "version": 3,
                "selected": FIRST,
                "active_workflow": "../invalid",
                "enabled": True,
            },
            True,
        ),
    ],
)
def test_legacy_documents_preserve_selection_and_enabled_without_active_target(
    state: WorkflowLocalState, document: dict, enabled: bool
) -> None:
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(json.dumps(document), encoding="utf-8")

    assert state.selected_id() == FIRST
    assert state.is_enabled() is enabled

    state.set_selected(SECOND)
    written = json.loads(state.path.read_text(encoding="utf-8"))
    assert written == {
        "version": 4,
        "workspace": str(state._workspace_root),
        "selected": SECOND,
        "enabled": enabled,
    }
    assert "active_workflow" not in written
    assert "available" not in written


def test_state_is_private_per_workspace_and_separate_from_agent_roster(
    tmp_path: Path,
) -> None:
    here = WorkflowLocalState(tmp_path / "here", state_root=tmp_path / "userdata")
    there = WorkflowLocalState(tmp_path / "there", state_root=tmp_path / "userdata")
    agents = AgentLocalState(tmp_path / "here", state_root=tmp_path / "userdata")
    here.set_selected(FIRST)
    here.set_enabled(True)

    assert here.path != there.path
    assert here.path != agents.path
    assert there.selected_id() == ""
    assert there.is_enabled() is False
    assert agents.available_ids() == ()
    assert not (tmp_path / "here" / ".aura").exists()


def test_a_corrupt_state_refuses_writes_without_losing_the_gate(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(WorkflowLocalStateError):
        state.set_enabled(True)
    assert state.selected_id() == ""
    assert state.is_enabled() is False


def test_project_workflow_cannot_switch_itself_on_or_grant_anything(
    tmp_path: Path,
) -> None:
    store = AgentGraphStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")
    state = WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    graph = store.create(AgentScope.PROJECT, name="Release review")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["enabled"] = True
    document["permissions"] = {"reviewer0000": "read_write"}
    path.write_text(json.dumps(document), encoding="utf-8")

    row = store.summary(graph.graph_id)
    assert row is not None and row.valid is False
    assert state.is_enabled() is False


def test_switching_agents_on_never_changes_a_workflow_document(tmp_path: Path) -> None:
    store = AgentGraphStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")
    state = WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    graph = store.create(AgentScope.PROJECT, name="Release review")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)
    before = path.read_text(encoding="utf-8")

    state.set_selected(graph.graph_id)
    state.set_enabled(True)

    assert path.read_text(encoding="utf-8") == before
    assert "enabled" not in before


def test_session_selection_changes_only_editor_state_and_keeps_gate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    personal = tmp_path / "personal"
    userdata = tmp_path / "userdata"
    store = AgentGraphStore(workspace, personal_dir=personal)
    first = store.create(AgentScope.PROJECT, name="First workflow")
    second = store.create(AgentScope.PROJECT, name="Second workflow")
    state = WorkflowLocalState(workspace, state_root=userdata)
    session = WorkflowSession(
        workspace,
        store_factory=lambda _root: AgentGraphStore(workspace, personal_dir=personal),
        state_factory=lambda _root: WorkflowLocalState(workspace, state_root=userdata),
    )
    session.reload()
    assert session.graph_id == first.graph_id
    assert state.selected_id() == first.graph_id
    assert state.is_enabled() is False

    session.set_enabled(True)
    session.open(second.graph_id)

    assert session.graph_id == second.graph_id
    assert state.selected_id() == second.graph_id
    assert state.is_enabled() is True


def test_session_can_enable_agents_without_any_saved_workflow(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    userdata = tmp_path / "userdata"
    state = WorkflowLocalState(workspace, state_root=userdata)
    session = WorkflowSession(
        workspace,
        store_factory=lambda _root: AgentGraphStore(
            workspace, personal_dir=tmp_path / "personal"
        ),
        state_factory=lambda _root: WorkflowLocalState(workspace, state_root=userdata),
    )
    session.reload()
    session.set_enabled(True)

    assert session.graph_id == ""
    assert state.selected_id() == ""
    assert state.is_enabled() is True
