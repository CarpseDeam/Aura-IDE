"""Private per-user, per-workspace agent state.

The roster ("Available to Aura") and the permission grant for each agent are
decisions one person made about one project on one machine. They live under
that user's own Aura data directory, never inside the project, so a
definition committed to a repository arrives everywhere else inactive and
read-only until its new reader decides otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aura.agents.local_state import (
    DEFAULT_PERMISSION,
    PERMISSION_ORDER,
    AgentLocalState,
    AgentPermission,
    workspace_key,
)


@pytest.fixture()
def state(tmp_path: Path) -> AgentLocalState:
    return AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")


# ── defaults ─────────────────────────────────────────────────────────────────


def test_an_unknown_agent_is_inactive_and_read_only(state: AgentLocalState) -> None:
    assert state.available_ids() == ()
    assert state.is_available("neveragentid") is False
    assert state.permission("neveragentid") is AgentPermission.READ_ONLY
    assert DEFAULT_PERMISSION is AgentPermission.READ_ONLY


def test_a_project_definition_grants_itself_nothing(tmp_path: Path) -> None:
    """A project arriving with definitions changes nothing about local state."""
    workspace = tmp_path / "workspace"
    definitions = workspace / ".aura" / "agents" / "definitions"
    definitions.mkdir(parents=True)
    (definitions / "arrivedagentid.md").write_text(
        "---\nid: arrivedagentid\nname: Arrived\ndescription: d.\n---\n\nWork.\n",
        encoding="utf-8",
    )

    state = AgentLocalState(workspace, state_root=tmp_path / "userdata")

    assert state.is_available("arrivedagentid") is False
    assert state.permission("arrivedagentid") is AgentPermission.READ_ONLY


def test_there_are_exactly_two_permissions_least_authority_first() -> None:
    assert PERMISSION_ORDER == (
        AgentPermission.READ_ONLY,
        AgentPermission.READ_WRITE,
    )
    assert tuple(AgentPermission) == (
        AgentPermission.READ_ONLY,
        AgentPermission.READ_WRITE,
    )
    assert [permission.label for permission in PERMISSION_ORDER] == [
        "Read only",
        "Read / Write",
    ]


def test_read_write_is_one_grant_covering_editing_and_terminal() -> None:
    assert AgentPermission.READ_ONLY.allows_edit is False
    assert AgentPermission.READ_ONLY.allows_terminal is False
    assert AgentPermission.READ_WRITE.allows_edit is True
    assert AgentPermission.READ_WRITE.allows_terminal is True


def test_an_older_grant_is_normalized_to_read_write_and_rewritten(
    tmp_path: Path,
) -> None:
    """Both former worktree grants mean Read / Write, and are persisted as it."""
    state = AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(
        json.dumps(
            {
                "available": ["editoragentid", "runneragentid"],
                "permissions": {
                    "editoragentid": "worktree_edit",
                    "runneragentid": "worktree_edit_terminal",
                },
            }
        ),
        encoding="utf-8",
    )

    assert state.permission("editoragentid") is AgentPermission.READ_WRITE
    assert state.permission("runneragentid") is AgentPermission.READ_WRITE

    # The next write persists only the current vocabulary — there is no
    # second representation left on disk to keep in step.
    state.set_available("thirdagentid", True)
    written = json.loads(state.path.read_text(encoding="utf-8"))
    assert written["version"] == 2
    assert written["permissions"] == {
        "editoragentid": "read_write",
        "runneragentid": "read_write",
    }
    assert "worktree_edit" not in state.path.read_text(encoding="utf-8")


# ── the roster ───────────────────────────────────────────────────────────────


def test_availability_keeps_the_order_the_user_built(state: AgentLocalState) -> None:
    state.set_available("firstagentid", True)
    state.set_available("secondagentid", True)
    state.set_available("thirdagentid", True)

    assert state.available_ids() == ("firstagentid", "secondagentid", "thirdagentid")

    state.set_available("secondagentid", False)
    assert state.available_ids() == ("firstagentid", "thirdagentid")

    state.set_available("secondagentid", True)
    assert state.available_ids() == ("firstagentid", "thirdagentid", "secondagentid")


def test_repeating_a_decision_changes_nothing(state: AgentLocalState) -> None:
    state.set_available("onlyagentid", True)
    state.set_available("onlyagentid", True)
    state.set_available("missingagentid", False)

    assert state.available_ids() == ("onlyagentid",)


def test_the_roster_can_be_replaced_wholesale(state: AgentLocalState) -> None:
    state.set_available("firstagentid", True)

    state.set_available_ids(["thirdagentid", "secondagentid", "thirdagentid"])

    assert state.available_ids() == ("thirdagentid", "secondagentid")


# ── permission ───────────────────────────────────────────────────────────────


def test_a_grant_round_trips(state: AgentLocalState) -> None:
    state.set_permission("workeragentid", AgentPermission.READ_WRITE)
    assert state.permission("workeragentid") is AgentPermission.READ_WRITE

    state.set_permission("workeragentid", AgentPermission.READ_WRITE)
    assert state.permission("workeragentid") is AgentPermission.READ_WRITE

    state.set_permission("workeragentid", AgentPermission.READ_ONLY)
    assert state.permission("workeragentid") is AgentPermission.READ_ONLY


def test_a_deleted_agent_leaves_no_authority_behind(state: AgentLocalState) -> None:
    state.set_available("goneagentid", True)
    state.set_permission("goneagentid", AgentPermission.READ_WRITE)

    state.forget("goneagentid")

    assert state.available_ids() == ()
    assert state.permission("goneagentid") is AgentPermission.READ_ONLY


def test_an_unreadable_grant_falls_back_to_read_only(tmp_path: Path) -> None:
    state = AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    state.set_permission("okagentid", AgentPermission.READ_WRITE)
    state.path.write_text(
        json.dumps({"available": ["okagentid"], "permissions": {"okagentid": "root"}}),
        encoding="utf-8",
    )

    assert state.permission("okagentid") is AgentPermission.READ_ONLY


def test_corrupt_state_does_not_crash_or_grant(tmp_path: Path) -> None:
    state = AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "userdata")
    state.set_permission("okagentid", AgentPermission.READ_WRITE)
    state.path.write_text("{ not json", encoding="utf-8")

    assert state.available_ids() == ()
    assert state.permission("okagentid") is AgentPermission.READ_ONLY


# ── privacy and isolation ────────────────────────────────────────────────────


def test_state_never_lands_inside_the_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = AgentLocalState(workspace, state_root=tmp_path / "userdata")

    state.set_available("privateagentid", True)
    state.set_permission("privateagentid", AgentPermission.READ_WRITE)

    assert state.path.is_file()
    assert tmp_path / "userdata" in state.path.parents
    assert list(workspace.rglob("*")) == []


def test_two_workspaces_do_not_share_decisions(tmp_path: Path) -> None:
    userdata = tmp_path / "userdata"
    first = AgentLocalState(tmp_path / "one", state_root=userdata)
    second = AgentLocalState(tmp_path / "two", state_root=userdata)

    first.set_available("sharedagentid", True)
    first.set_permission("sharedagentid", AgentPermission.READ_WRITE)

    assert second.available_ids() == ()
    assert second.permission("sharedagentid") is AgentPermission.READ_ONLY
    assert first.path != second.path


def test_the_same_workspace_reopens_its_own_decisions(tmp_path: Path) -> None:
    userdata = tmp_path / "userdata"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    AgentLocalState(workspace, state_root=userdata).set_permission(
        "keptagentid", AgentPermission.READ_WRITE
    )

    reopened = AgentLocalState(workspace, state_root=userdata)

    assert reopened.permission("keptagentid") is AgentPermission.READ_WRITE


def test_the_workspace_key_is_stable_and_path_shaped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    key = workspace_key(workspace)

    assert key == workspace_key(workspace)
    assert key != workspace_key(tmp_path / "other")
    assert key.isalnum()


def test_the_file_records_which_workspace_it_belongs_to(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = AgentLocalState(workspace, state_root=tmp_path / "userdata")

    state.set_available("recordedagentid", True)
    written = json.loads(state.path.read_text(encoding="utf-8"))

    assert written["workspace"] == str(workspace)
    assert written["available"] == ["recordedagentid"]
