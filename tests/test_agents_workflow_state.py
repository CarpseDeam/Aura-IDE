"""Private per-user, per-workspace workflow state.

Which workflow this person has open, and whether Aura may run it during an
ordinary conversation, are decisions one person made about one project on one
machine. They live under that user's own Aura data directory, never inside the
project, so a workflow committed to a repository arrives everywhere else
switched off until its new reader decides otherwise.

There is exactly one enabled bit, and it belongs to whatever is selected —
never a flag per workflow as well, because two answers to one question is how
a user ends up unable to tell which one Aura believed.
"""
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


# ── defaults ─────────────────────────────────────────────────────────────────


def test_nothing_is_selected_and_nothing_is_enabled(state: WorkflowLocalState) -> None:
    assert state.selected_id() == ""
    assert state.is_enabled() is False


def test_the_switch_says_out_loud_that_it_is_a_personal_choice() -> None:
    assert "never written into a workflow" in ENABLED_NOTE


# ── selection and the gate ───────────────────────────────────────────────────


def test_the_open_workflow_is_remembered_and_can_be_cleared(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    assert state.selected_id() == FIRST

    state.set_selected("")
    assert state.selected_id() == ""


def test_the_gate_round_trips_for_the_selected_workflow(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)

    state.set_enabled(True)
    assert state.is_enabled() is True

    state.set_enabled(False)
    assert state.is_enabled() is False


def test_the_gate_cannot_be_switched_on_with_nothing_selected(
    state: WorkflowLocalState,
) -> None:
    state.set_enabled(True)

    assert state.is_enabled() is False
    assert state.selected_id() == ""


def test_selecting_another_workflow_switches_the_gate_off(
    state: WorkflowLocalState,
) -> None:
    """The decision was about that workflow, and does not follow the cursor."""
    state.set_selected(FIRST)
    state.set_enabled(True)

    state.set_selected(SECOND)

    assert state.selected_id() == SECOND
    assert state.is_enabled() is False


def test_forgetting_the_selected_workflow_clears_the_gate(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_enabled(True)

    state.forget(FIRST)

    assert state.selected_id() == ""
    assert state.is_enabled() is False


def test_forgetting_another_workflow_leaves_the_selection_alone(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_enabled(True)

    state.forget(SECOND)

    assert state.selected_id() == FIRST
    assert state.is_enabled() is True


@pytest.mark.parametrize("raw", ["no", "../escape", "has/slash", "UPPER"])
def test_an_unusable_workflow_id_is_refused(
    state: WorkflowLocalState, raw: str
) -> None:
    with pytest.raises(WorkflowLocalStateError):
        state.set_selected(raw)


# ── the older per-workflow availability list ─────────────────────────────────


def test_the_selected_version_one_availability_choice_migrates_to_the_master_gate(
    state: WorkflowLocalState,
) -> None:
    """One-way normalization preserves only the selected workflow's choice."""
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(
        json.dumps({"version": 1, "available": [FIRST, SECOND], "selected": FIRST}),
        encoding="utf-8",
    )

    assert state.selected_id() == FIRST
    assert state.is_enabled() is True

    state.set_enabled(False)
    written = json.loads(state.path.read_text(encoding="utf-8"))
    assert "available" not in written
    assert written["enabled"] is False
    assert written["selected"] == FIRST


# ── where it lives, and what it never touches ────────────────────────────────


def test_the_state_file_is_never_inside_the_project(
    state: WorkflowLocalState, tmp_path: Path
) -> None:
    state.set_selected(FIRST)

    assert state.path.is_file()
    assert tmp_path / "userdata" in state.path.parents
    assert not (tmp_path / "workspace" / ".aura").exists()


def test_two_workspaces_never_share_a_file(tmp_path: Path) -> None:
    here = WorkflowLocalState(tmp_path / "here", state_root=tmp_path / "userdata")
    there = WorkflowLocalState(tmp_path / "there", state_root=tmp_path / "userdata")

    here.set_selected(FIRST)
    here.set_enabled(True)

    assert here.path != there.path
    assert there.selected_id() == ""
    assert there.is_enabled() is False


def test_workflow_state_is_a_separate_file_from_the_agent_roster(
    tmp_path: Path,
) -> None:
    workflows = WorkflowLocalState(
        tmp_path / "workspace", state_root=tmp_path / "userdata"
    )
    agents = AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")

    workflows.set_selected(FIRST)

    assert workflows.path != agents.path
    assert agents.available_ids() == ()


def test_a_corrupt_state_file_refuses_a_write_rather_than_losing_it(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(WorkflowLocalStateError):
        state.set_selected(SECOND)

    assert state.selected_id() == ""
    assert state.is_enabled() is False


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
    document["enabled"] = True
    document["permissions"] = {"reviewer0000": "read_write"}
    path.write_text(json.dumps(document), encoding="utf-8")

    row = store.summary(graph.graph_id)

    assert row is not None and row.valid is False
    assert state.is_enabled() is False
    assert state.selected_id() == ""


def test_switching_a_workflow_on_writes_nothing_into_the_project(
    tmp_path: Path,
) -> None:
    store = AgentGraphStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")
    state = WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    graph = store.create(AgentScope.PROJECT, name="Release review")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)
    before = path.read_text(encoding="utf-8")

    state.set_selected(graph.graph_id)
    state.set_enabled(True)

    assert state.is_enabled() is True
    assert path.read_text(encoding="utf-8") == before
    assert "enabled" not in before


def test_the_first_discovered_workflow_becomes_the_private_selection(
    tmp_path: Path,
) -> None:
    """A visible fallback selection must be a selection the gate can own."""
    workspace = tmp_path / "workspace"
    personal = tmp_path / "personal"
    userdata = tmp_path / "userdata"
    store = AgentGraphStore(workspace, personal_dir=personal)
    graph = store.create(AgentScope.PROJECT, name="First workflow")
    state = WorkflowLocalState(workspace, state_root=userdata)
    session = WorkflowSession(
        workspace,
        store_factory=lambda _root: AgentGraphStore(workspace, personal_dir=personal),
        state_factory=lambda _root: WorkflowLocalState(workspace, state_root=userdata),
    )

    session.reload()

    assert session.graph_id == graph.graph_id
    assert state.selected_id() == graph.graph_id
    assert state.is_enabled() is False
