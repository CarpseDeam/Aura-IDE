from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from aura.drones.workspaces.model import WorkspacePhase
from aura.drones.workspaces.store import DroneWorkspaceStore
from aura.gui.drones.drone_workspace_pane import DroneWorkspacePane, _WorkspaceRow


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_workspace_pane_emits_workspace_id_for_builder_row(
    qapp: QApplication, tmp_path: Path
) -> None:
    workspace = DroneWorkspaceStore.create_workspace(tmp_path, "Draft Drone")
    pane = DroneWorkspacePane(tmp_path)
    selected: list[str] = []
    pane.workspace_selected.connect(selected.append)

    pane.refresh()
    row = pane.findChild(_WorkspaceRow)
    assert row is not None

    row.clicked.emit(workspace.workspace_id)

    assert selected == [workspace.workspace_id]


def test_workspace_pane_emits_workspace_id_for_installed_row(
    qapp: QApplication, tmp_path: Path
) -> None:
    workspace = DroneWorkspaceStore.create_workspace(
        tmp_path,
        "Ready Drone",
        mode="edit",
        installed_drone_id="ready-drone",
    )
    workspace.phase = WorkspacePhase.READY.value
    DroneWorkspaceStore.save_workspace(workspace)
    pane = DroneWorkspacePane(tmp_path)
    selected: list[str] = []
    pane.workspace_selected.connect(selected.append)

    pane.refresh()
    row = pane.findChild(_WorkspaceRow)
    assert row is not None

    row.clicked.emit(workspace.workspace_id)

    assert selected == [workspace.workspace_id]
