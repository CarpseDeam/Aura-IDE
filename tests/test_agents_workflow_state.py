"""Private per-user, per-workspace workflow conversation state.

The editor selection, the saved workflow Aura may use, and the global Agents
switch are three distinct facts. Browsing changes only the editor selection.
An enabled state with no active saved workflow is automatic team assembly.
Everything remains private to this user and workspace.
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


def test_nothing_is_selected_active_or_enabled(state: WorkflowLocalState) -> None:
    assert state.selected_id() == ""
    assert state.active_workflow_id() == ""
    assert state.is_enabled() is False


def test_the_switch_says_out_loud_that_it_is_a_personal_choice() -> None:
    assert "never written into a workflow" in ENABLED_NOTE
    assert "assemble a team" in ENABLED_NOTE


# ── editor selection, active target, and the gate ──────────────────────────────


def test_the_open_workflow_is_remembered_and_can_be_cleared(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    assert state.selected_id() == FIRST

    state.set_selected("")
    assert state.selected_id() == ""


def test_the_active_workflow_is_separate_and_can_be_cleared(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_active_workflow(SECOND)

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == SECOND

    state.set_active_workflow("")

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == ""


def test_the_global_gate_round_trips_without_an_active_workflow(
    state: WorkflowLocalState,
) -> None:
    state.set_enabled(True)

    assert state.is_enabled() is True
    assert state.selected_id() == ""
    assert state.active_workflow_id() == ""

    state.set_enabled(False)
    assert state.is_enabled() is False


def test_enabled_with_no_active_workflow_means_automatic_assembly(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_active_workflow("")
    state.set_enabled(True)

    assert state.is_enabled() is True
    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == ""


def test_browsing_another_workflow_does_not_redirect_or_disable_the_active_one(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_active_workflow(FIRST)
    state.set_enabled(True)

    state.set_selected(SECOND)

    assert state.selected_id() == SECOND
    assert state.active_workflow_id() == FIRST
    assert state.is_enabled() is True


def test_clearing_the_editor_selection_does_not_clear_the_active_workflow(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_active_workflow(FIRST)
    state.set_enabled(True)

    state.set_selected("")

    assert state.selected_id() == ""
    assert state.active_workflow_id() == FIRST
    assert state.is_enabled() is True


def test_disabling_keeps_the_active_choice_for_the_next_enable(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(FIRST)
    state.set_active_workflow(FIRST)
    state.set_enabled(True)

    state.set_enabled(False)

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == FIRST
    assert state.is_enabled() is False


def test_forgetting_the_active_workflow_turns_agents_off_but_keeps_other_selection(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(SECOND)
    state.set_active_workflow(FIRST)
    state.set_enabled(True)

    state.forget(FIRST)

    assert state.selected_id() == SECOND
    assert state.active_workflow_id() == ""
    assert state.is_enabled() is False


def test_forgetting_only_the_selected_workflow_does_not_touch_active_authority(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(SECOND)
    state.set_active_workflow(FIRST)
    state.set_enabled(True)

    state.forget(SECOND)

    assert state.selected_id() == ""
    assert state.active_workflow_id() == FIRST
    assert state.is_enabled() is True


def test_forgetting_an_unrelated_workflow_changes_nothing(
    state: WorkflowLocalState,
) -> None:
    state.set_selected(SECOND)
    state.set_active_workflow(FIRST)
    state.set_enabled(True)

    state.forget("workflowthree3")

    assert state.selected_id() == SECOND
    assert state.active_workflow_id() == FIRST
    assert state.is_enabled() is True


@pytest.mark.parametrize("raw", ["no", "../escape", "has/slash", "UPPER"])
@pytest.mark.parametrize("setter", ["set_selected", "set_active_workflow"])
def test_an_unusable_workflow_id_is_refused_for_each_identity(
    state: WorkflowLocalState, raw: str, setter: str
) -> None:
    with pytest.raises(WorkflowLocalStateError):
        getattr(state, setter)(raw)


# ── the older per-workflow availability list ─────────────────────────────────


def test_the_selected_version_one_availability_choice_migrates_to_the_master_gate(
    state: WorkflowLocalState,
) -> None:
    """The enabled v1 selection becomes the explicit active target."""
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(
        json.dumps({"version": 1, "available": [FIRST, SECOND], "selected": FIRST}),
        encoding="utf-8",
    )

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == FIRST
    assert state.is_enabled() is True

    state.set_enabled(False)
    written = json.loads(state.path.read_text(encoding="utf-8"))
    assert "available" not in written
    assert written["version"] == 3
    assert written["enabled"] is False
    assert written["selected"] == FIRST
    assert written["active_workflow"] == FIRST


def test_a_disabled_version_one_selection_does_not_gain_active_authority(
    state: WorkflowLocalState,
) -> None:
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(
        json.dumps({"version": 1, "available": [SECOND], "selected": FIRST}),
        encoding="utf-8",
    )

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == ""
    assert state.is_enabled() is False


def test_an_enabled_version_two_selection_migrates_to_the_active_target(
    state: WorkflowLocalState,
) -> None:
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(
        json.dumps({"version": 2, "selected": FIRST, "enabled": True}),
        encoding="utf-8",
    )

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == FIRST
    assert state.is_enabled() is True

    # Browsing after migration writes v3 without redirecting the migrated target.
    state.set_selected(SECOND)
    written = json.loads(state.path.read_text(encoding="utf-8"))
    assert written["version"] == 3
    assert written["selected"] == SECOND
    assert written["active_workflow"] == FIRST
    assert written["enabled"] is True


def test_a_disabled_version_two_selection_does_not_become_active(
    state: WorkflowLocalState,
) -> None:
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(
        json.dumps({"version": 2, "selected": FIRST, "enabled": False}),
        encoding="utf-8",
    )

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == ""
    assert state.is_enabled() is False


def test_a_malformed_v3_active_target_fails_closed_instead_of_becoming_automatic(
    state: WorkflowLocalState,
) -> None:
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(
        json.dumps(
            {
                "version": 3,
                "selected": FIRST,
                "active_workflow": "../not-a-workflow",
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )

    assert state.selected_id() == FIRST
    assert state.active_workflow_id() == ""
    assert state.is_enabled() is False


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
    assert here.active_workflow_id() == ""
    assert here.is_enabled() is True
    assert there.selected_id() == ""
    assert there.active_workflow_id() == ""
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
    assert state.active_workflow_id() == ""
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
    assert state.active_workflow_id() == ""


def test_switching_a_workflow_on_writes_nothing_into_the_project(
    tmp_path: Path,
) -> None:
    store = AgentGraphStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")
    state = WorkflowLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    graph = store.create(AgentScope.PROJECT, name="Release review")
    path = store.path_for(AgentScope.PROJECT, graph.graph_id)
    before = path.read_text(encoding="utf-8")

    state.set_selected(graph.graph_id)
    state.set_active_workflow(graph.graph_id)
    state.set_enabled(True)

    assert state.is_enabled() is True
    assert state.active_workflow_id() == graph.graph_id
    assert path.read_text(encoding="utf-8") == before
    assert "enabled" not in before


def test_the_first_discovered_workflow_becomes_the_private_selection(
    tmp_path: Path,
) -> None:
    """A fallback opens the editor without silently activating that workflow."""
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
    assert state.active_workflow_id() == ""
    assert state.is_enabled() is False


def test_session_activation_freezes_the_open_workflow_while_browsing_moves_on(
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

    session.set_enabled(True)
    session.open(second.graph_id)

    assert session.graph_id == second.graph_id
    assert state.selected_id() == second.graph_id
    assert state.active_workflow_id() == first.graph_id
    assert state.is_enabled() is True


def test_session_can_enable_automatic_assembly_without_any_saved_workflow(
    tmp_path: Path,
) -> None:
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
    assert state.active_workflow_id() == ""
    assert state.is_enabled() is True
