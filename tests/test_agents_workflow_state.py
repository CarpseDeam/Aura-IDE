"""Private per-user, per-workspace workflow state.

Whether Aura may call a workflow, and which one this person last had open,
are decisions one person made about one project on one machine. They live
under that user's own Aura data directory, never inside the project, so a
workflow committed to a repository arrives everywhere else switched off until
its new reader decides otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.agents.graph_local_state import (
    AVAILABILITY_NOTE,
    WorkflowLocalState,
    WorkflowLocalStateError,
)
from aura.agents.graph_store import AgentGraphStore
from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentLocalState

FIRST = "workflowone1"
SECOND = "workflowtwo2"


@pytest.fixture()
def state(tmp_path: Path) -> WorkflowLocalState:
    return WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")


# ── defaults ─────────────────────────────────────────────────────────────────


def test_an_unknown_workflow_is_switched_off(state: WorkflowLocalState) -> None:
    assert state.available_ids() == ()
    assert state.is_available(FIRST) is False
    assert state.selected_id() == ""


def test_the_switch_says_out_loud_that_it_is_a_personal_choice() -> None:
    assert "never written into a workflow" in AVAILABILITY_NOTE


# ── available to Aura ────────────────────────────────────────────────────────


def test_switching_workflows_on_keeps_the_order_they_were_switched_on(
    state: WorkflowLocalState,
) -> None:
    state.set_available(SECOND, True)
    state.set_available(FIRST, True)

    assert state.available_ids() == (SECOND, FIRST)

    state.set_available(SECOND, False)

    assert state.available_ids() == (FIRST,)
    assert state.is_available(SECOND) is False


def test_switching_one_on_twice_changes_nothing(state: WorkflowLocalState) -> None:
    state.set_available(FIRST, True)
    state.set_available(FIRST, True)

    assert state.available_ids() == (FIRST,)


def test_the_open_workflow_is_remembered_and_can_be_cleared(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    assert state.selected_id() == FIRST

    state.set_selected("")
    assert state.selected_id() == ""


def test_forgetting_a_workflow_drops_every_decision_about_it(
    state: WorkflowLocalState,
) -> None:
    state.set_available(FIRST, True)
    state.set_selected(FIRST)

    state.forget(FIRST)

    assert state.available_ids() == ()
    assert state.selected_id() == ""


@pytest.mark.parametrize("raw", ["", "no", "../escape", "has/slash", "UPPER"])
def test_an_unusable_workflow_id_is_refused(
    state: WorkflowLocalState, raw: str
) -> None:
    with pytest.raises(WorkflowLocalStateError):
        state.set_available(raw, True)


# ── where it lives, and what it never touches ────────────────────────────────


def test_the_state_file_is_never_inside_the_project(
    state: WorkflowLocalState, tmp_path: Path
) -> None:
    state.set_available(FIRST, True)

    assert state.path.is_file()
    assert tmp_path / "userdata" in state.path.parents
    assert not (tmp_path / "workspace" / ".aura").exists()


def test_two_workspaces_never_share_a_file(tmp_path: Path) -> None:
    here = WorkflowLocalState(tmp_path / "here", state_root=tmp_path / "userdata")
    there = WorkflowLocalState(tmp_path / "there", state_root=tmp_path / "userdata")

    here.set_available(FIRST, True)

    assert here.path != there.path
    assert there.available_ids() == ()


def test_workflow_state_is_a_separate_file_from_the_agent_roster(
    tmp_path: Path,
) -> None:
    workflows = WorkflowLocalState(
        tmp_path / "workspace", state_root=tmp_path / "userdata"
    )
    agents = AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")

    workflows.set_available(FIRST, True)

    assert workflows.path != agents.path
    assert agents.available_ids() == ()


def test_a_corrupt_state_file_refuses_a_write_rather_than_losing_it(
    state: WorkflowLocalState,
) -> None:
    state.set_available(FIRST, True)
    state.path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(WorkflowLocalStateError):
        state.set_available(SECOND, True)

    assert state.available_ids() == ()


# ── a project workflow cannot switch itself on ───────────────────────────────


def test_a_project_workflow_cannot_switch_itself_on_or_grant_anything(
    tmp_path: Path,
) -> None:
    store = AgentGraphStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")
    state = WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    graph = store.create(AgentScope.PROJECT, name="Release review")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)

    # Somebody commits a workflow that claims to be switched on and to grant
    # its agents authority. The reader's own state is what decides, and the
    # file is refused rather than half-honoured.
    document = json.loads(path.read_text(encoding="utf-8"))
    document["available"] = True
    document["permissions"] = {"reviewer0000": "worktree_edit_terminal"}
    path.write_text(json.dumps(document), encoding="utf-8")

    row = store.summary(graph.graph_id)

    assert row is not None and row.valid is False
    assert state.is_available(graph.graph_id) is False
    assert state.available_ids() == ()


def test_switching_a_workflow_on_writes_nothing_into_the_project(
    tmp_path: Path,
) -> None:
    store = AgentGraphStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")
    state = WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    graph = store.create(AgentScope.PROJECT, name="Release review")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)
    before = path.read_text(encoding="utf-8")

    state.set_available(graph.graph_id, True)

    assert state.is_available(graph.graph_id) is True
    assert path.read_text(encoding="utf-8") == before
    assert "available" not in before
