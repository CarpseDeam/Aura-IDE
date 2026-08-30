"""Security and persistence boundaries for Agent definitions and local state."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.agents.identity import AgentScope  # noqa: E402
from aura.agents.local_state import (  # noqa: E402
    AgentLocalState,
    AgentLocalStateError,
    AgentPermission,
)
from aura.agents.models import AgentDefinition  # noqa: E402
from aura.agents.store import AgentStore, AgentStoreError  # noqa: E402
from aura.agents.validation import (  # noqa: E402
    MAX_AGENT_DESCRIPTION_CHARS,
    delegation_description_error,
)
from aura.gui.agents_editor import AgentEditor, ModelChoices  # noqa: E402
from aura.gui.main_window_agents import MainWindowAgentsController  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _definition(agent_id: str, *, description: str = "Reviews one change.") -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        scope=AgentScope.PROJECT,
        name="Reviewer",
        description=description,
        instructions="Review carefully.",
    )


@pytest.mark.parametrize("operation", ["path", "get", "update", "delete"])
def test_public_definition_paths_refuse_invalid_immutable_ids(
    tmp_path: Path, operation: str
) -> None:
    store = AgentStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")

    with pytest.raises(AgentStoreError, match="valid immutable agent id"):
        if operation == "path":
            store.path_for(AgentScope.PROJECT, "../escape")
        elif operation == "get":
            store.get("../escape")
        elif operation == "update":
            store.update(_definition("../escape"))
        else:
            store.delete(AgentScope.PROJECT, "../escape")


def test_link_like_definition_ancestor_blocks_discovery_update_and_delete(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AgentStore(workspace, personal_dir=tmp_path / "personal")
    created = store.create(
        AgentScope.PROJECT,
        name="Reviewer",
        description="Reviews one change.",
        instructions="Review carefully.",
    )
    real_aura = tmp_path / "real-aura"
    (workspace / ".aura").rename(real_aura)
    try:
        os.symlink(real_aura, workspace / ".aura", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    assert store.list_summaries() == ()
    with pytest.raises(AgentStoreError, match="symlink, junction"):
        store.create(
            AgentScope.PROJECT,
            name="Another",
            description="Reviews one change.",
            instructions="Review carefully.",
        )
    with pytest.raises(AgentStoreError, match="symlink, junction"):
        store.update(_definition(created.agent_id))
    with pytest.raises(AgentStoreError, match="symlink, junction"):
        store.delete(AgentScope.PROJECT, created.agent_id)


def test_local_state_write_failures_propagate_without_changing_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "state")

    def fail(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("aura.agents.local_state.atomic_write_bytes", fail)
    with pytest.raises(AgentLocalStateError, match="disk full"):
        state.set_available("agent0001", True)

    fresh = AgentLocalState(tmp_path / "workspace", state_root=tmp_path / "state")
    assert fresh.available_ids() == ()


def test_gui_rolls_back_roster_and_permission_when_persistence_fails(
    tmp_path: Path, qapp
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AgentStore(workspace, personal_dir=tmp_path / "personal")
    created = store.create(
        AgentScope.PROJECT,
        name="Reviewer",
        description="Reviews one change.",
        instructions="Review carefully.",
    )

    class FailingState(AgentLocalState):
        def set_available(self, agent_id: str, available: bool) -> None:
            raise AgentLocalStateError("could not persist roster")

        def set_permission(self, agent_id: str, permission: AgentPermission) -> None:
            raise AgentLocalStateError("could not persist permission")

    state = FailingState(workspace, state_root=tmp_path / "state")
    window = SimpleNamespace(_workspace_root=workspace, _edge_rail=None)
    controller = MainWindowAgentsController(
        window,
        workspace_root=workspace,
        store_factory=lambda _root: store,
        state_factory=lambda _root: state,
        choices=ModelChoices(),
    )
    page = controller._ensure_page()
    controller.refresh()
    page.select_agent(created.agent_id)

    source_key = f"project:{created.agent_id}"
    page._items[source_key].setCheckState(0, Qt.CheckState.Checked)
    assert page._items[source_key].checkState(0) == Qt.CheckState.Unchecked

    index = page._permission.findData(AgentPermission.READ_WRITE.value)
    page._permission.setCurrentIndex(index)
    assert page._permission.currentData() == AgentPermission.READ_ONLY.value
    page.close()
    page.deleteLater()


def test_description_rule_is_shared_by_validation_and_editor(qapp) -> None:
    assert delegation_description_error("two\nlines")
    assert delegation_description_error("x" * (MAX_AGENT_DESCRIPTION_CHARS + 1))
    assert delegation_description_error("x" * MAX_AGENT_DESCRIPTION_CHARS) == ""

    editor = AgentEditor(ModelChoices())
    assert editor.description.maxLength() == MAX_AGENT_DESCRIPTION_CHARS
    editor.deleteLater()


@pytest.mark.parametrize(
    "description",
    ["two\nlines", "x" * (MAX_AGENT_DESCRIPTION_CHARS + 1)],
)
def test_store_enforces_the_shared_short_single_line_description(
    tmp_path: Path, description: str
) -> None:
    store = AgentStore(tmp_path / "workspace", personal_dir=tmp_path / "personal")
    with pytest.raises(AgentStoreError):
        store.create(
            AgentScope.PROJECT,
            name="Reviewer",
            description=description,
            instructions="Review carefully.",
        )
