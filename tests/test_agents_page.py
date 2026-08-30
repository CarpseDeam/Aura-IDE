"""The Agents page and its controller: opening, CRUD, roster, and grants.

The controller is the only thing here that touches disk. The page shows what
it is handed and emits what the user did, so every assertion below is either
about what a user sees or about what actually landed in a definition file or
in this user's private local state — never about a value the page invented.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from aura.agents.graph_local_state import WorkflowLocalState  # noqa: E402
from aura.agents.graph_store import AgentGraphStore  # noqa: E402
from aura.agents.local_state import AgentLocalState, AgentPermission  # noqa: E402
from aura.agents.models import AgentScope, AgentThinking  # noqa: E402
from aura.agents.store import AgentStore  # noqa: E402
from aura.gui.agents_page import ModelChoices  # noqa: E402
from aura.gui.main_window_agents import MainWindowAgentsController  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Tab:
    def __init__(self) -> None:
        self.checked = False

    def setChecked(self, value: bool) -> None:  # noqa: N802 - Qt naming
        self.checked = bool(value)

    def isChecked(self) -> bool:  # noqa: N802 - Qt naming
        return self.checked


#: A fixed model list for Aura's pretend current provider, so the model
#: control is asserted against known options rather than whatever the real
#: catalog happens to hold. There is no provider list, because an agent has
#: no provider to choose.
_CHOICES = ModelChoices(
    models=("claude-sonnet-4-6", "gpt-5.5"),
    current_model="claude-sonnet-4-6",
)


@pytest.fixture()
def wired(tmp_path: Path, qapp, monkeypatch) -> SimpleNamespace:
    """The controller as MainWindow builds it, against a throwaway workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    personal = tmp_path / "personal"
    workflows = tmp_path / "workflows"
    userdata = tmp_path / "userdata"

    tab = _Tab()
    window = SimpleNamespace(_workspace_root=workspace, _edge_rail=SimpleNamespace(agents_tab=tab))
    controller = MainWindowAgentsController(
        window,
        workspace_root=workspace,
        store_factory=lambda root: AgentStore(root, personal_dir=personal),
        state_factory=lambda root: AgentLocalState(root, state_root=userdata),
        graph_store_factory=lambda root: AgentGraphStore(root, personal_dir=workflows),
        workflow_state_factory=lambda root: WorkflowLocalState(
            root, state_root=userdata
        ),
        choices=_CHOICES,
    )
    # Deleting is destructive, so the controller asks first; say yes.
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    harness = SimpleNamespace(
        workspace=workspace,
        controller=controller,
        tab=tab,
        store=AgentStore(workspace, personal_dir=personal),
        state=AgentLocalState(workspace, state_root=userdata),
    )
    yield harness
    page = controller.agents_page
    if page is not None:
        page.close()
        page.deleteLater()


def _seed(store: AgentStore, scope: AgentScope, name: str) -> str:
    definition = store.create(
        scope, name=name, description=f"{name} does one thing.", instructions=f"Be {name}."
    )
    return definition.agent_id


# ── opening and toggling ─────────────────────────────────────────────────────


def test_the_page_opens_toggles_and_tracks_the_rail(wired) -> None:
    controller = wired.controller

    assert controller.agents_page is None
    assert controller.is_open() is False

    controller.on_agents_requested()
    assert controller.agents_page is not None
    assert controller.is_open() is True
    assert wired.tab.isChecked() is True

    first_page = controller.agents_page
    controller.on_agents_requested()
    assert controller.is_open() is False
    assert wired.tab.isChecked() is False

    controller.on_agents_requested()
    assert controller.agents_page is first_page
    assert controller.is_open() is True


def test_closing_the_page_unchecks_the_rail(wired) -> None:
    wired.controller.on_agents_requested()

    wired.controller.agents_page.close()

    assert wired.controller.is_open() is False
    assert wired.tab.isChecked() is False


def test_changing_workspace_hides_the_page_and_drops_the_roster(wired, tmp_path: Path) -> None:
    _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    assert wired.controller.agents_page.visible_agent_ids()["project"]

    wired.controller.hide_page()
    wired.controller.set_workspace_root(tmp_path / "elsewhere")

    assert wired.controller.is_open() is False
    assert wired.controller.agents_page.visible_agent_ids()["project"] == ()


# ── the two lists ────────────────────────────────────────────────────────────


def test_project_and_personal_agents_are_listed_separately(wired) -> None:
    project_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    personal_id = _seed(wired.store, AgentScope.PERSONAL, "Scout")

    wired.controller.on_agents_requested()
    visible = wired.controller.agents_page.visible_agent_ids()

    assert visible["project"] == (project_id,)
    assert visible["personal"] == (personal_id,)


def test_a_broken_definition_stays_visible_and_cannot_be_activated(wired) -> None:
    wired.store.project_dir.mkdir(parents=True, exist_ok=True)
    (wired.store.project_dir / "brokenagentid.md").write_text(
        "---\nid: brokenagentid\n---\n\nno name, no description\n", encoding="utf-8"
    )

    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    item = page._items["project:brokenagentid"]

    assert page.visible_agent_ids()["project"] == ("brokenagentid",)
    assert not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable)


# ── create, edit, delete ─────────────────────────────────────────────────────


def test_creating_an_agent_writes_a_definition_and_selects_it(wired) -> None:
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page

    page.create_requested.emit("project")

    definitions = wired.store.definitions()
    assert len(definitions) == 1
    created = definitions[0]
    assert created.scope is AgentScope.PROJECT
    assert page.current_agent_id() == created.agent_id
    assert wired.store.path_for(AgentScope.PROJECT, created.agent_id).is_file()


def test_a_personal_agent_is_created_outside_the_project(wired) -> None:
    wired.controller.on_agents_requested()

    wired.controller.agents_page.create_requested.emit("personal")

    (created,) = wired.store.definitions()
    assert created.scope is AgentScope.PERSONAL
    assert not (wired.workspace / ".aura" / "agents" / "definitions").exists()


def test_editing_and_saving_keeps_the_id_and_rewrites_the_file(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    page._name.setText("Auditor")
    page._description.setText("Audits a diff for defects.")
    page._instructions.setPlainText("Read the diff twice.")
    page._thinking.setCurrentIndex(page._thinking.findData(AgentThinking.MAX.value))
    page._save_btn.click()

    saved = wired.store.get(agent_id)
    assert saved is not None
    assert saved.agent_id == agent_id
    assert saved.name == "Auditor"
    assert saved.instructions == "Read the diff twice."
    assert saved.thinking is AgentThinking.MAX


def test_the_editor_has_a_model_control_and_no_provider_control(wired) -> None:
    """An Agent never pins a provider, so there is nowhere to pick one."""
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    assert page._model.isEnabled() is True
    assert not hasattr(page, "_provider")
    assert not hasattr(page._editor, "provider")
    assert "provider" not in {
        field.lower() for field in type(page.draft()).__dataclass_fields__
    }


def test_the_model_control_offers_the_current_providers_models(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    listed = [page._model.itemText(index) for index in range(page._model.count())]

    assert listed == [
        "Use Aura's current model (claude-sonnet-4-6)",
        "claude-sonnet-4-6",
        "gpt-5.5",
    ]
    assert page._model.itemData(0) == ""


def test_an_agent_with_no_model_keeps_the_inherit_choice_when_saved(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    assert wired.store.get(agent_id).model == ""
    assert page._model.currentData() == ""
    assert "aura's current model" in page._model.currentText().lower()

    page._save_btn.click()

    assert wired.store.get(agent_id).model == ""


def test_choosing_an_explicit_model_is_saved_without_a_provider(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    page._model.setCurrentText("gpt-5.5")
    page._save_btn.click()

    saved = wired.store.get(agent_id)
    assert saved is not None
    assert saved.model == "gpt-5.5"
    text = wired.store.path_for(AgentScope.PROJECT, agent_id).read_text(encoding="utf-8")
    assert "provider" not in text.lower()


def test_deleting_removes_the_file_and_every_local_decision(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)
    page.availability_changed.emit(agent_id, True)
    page.permission_changed.emit(agent_id, AgentPermission.READ_WRITE.value)

    page.select_agent(agent_id)
    page._delete_btn.click()

    assert wired.store.get(agent_id) is None
    assert wired.state.available_ids() == ()
    assert wired.state.permission(agent_id) is AgentPermission.READ_ONLY
    assert page.visible_agent_ids()["project"] == ()


# ── available to Aura, and what it may do ────────────────────────────────────


def test_a_newly_discovered_project_agent_starts_inactive_and_read_only(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Arrived")

    wired.controller.on_agents_requested()
    page = wired.controller.agents_page

    assert page._items[f"project:{agent_id}"].checkState(0) == Qt.CheckState.Unchecked
    assert wired.state.is_available(agent_id) is False
    assert wired.state.permission(agent_id) is AgentPermission.READ_ONLY
    assert page._permission.currentData() == AgentPermission.READ_ONLY.value


def test_ticking_an_agent_makes_it_available_in_order(wired) -> None:
    first = _seed(wired.store, AgentScope.PROJECT, "First")
    second = _seed(wired.store, AgentScope.PROJECT, "Second")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page

    before = page._items[f"project:{first}"]
    page._items[f"project:{second}"].setCheckState(0, Qt.CheckState.Checked)
    page._items[f"project:{first}"].setCheckState(0, Qt.CheckState.Checked)

    assert wired.state.available_ids() == (second, first)
    assert page._items[f"project:{first}"].checkState(0) == Qt.CheckState.Checked
    # The row is updated in place: rebuilding the tree from inside a check
    # box's own signal would destroy the item Qt is still emitting for.
    assert page._items[f"project:{first}"] is before

    page._items[f"project:{second}"].setCheckState(0, Qt.CheckState.Unchecked)
    assert wired.state.available_ids() == (first,)


def test_choosing_a_permission_records_it_locally_only(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    page._permission.setCurrentIndex(
        page._permission.findData(AgentPermission.READ_WRITE.value)
    )

    assert wired.state.permission(agent_id) is AgentPermission.READ_WRITE
    definition_text = wired.store.path_for(AgentScope.PROJECT, agent_id).read_text(
        encoding="utf-8"
    )
    assert "worktree" not in definition_text.lower()
    assert "permission" not in definition_text.lower()


def test_the_permission_control_offers_exactly_two_choices(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    listed = [
        (page._permission.itemText(index), page._permission.itemData(index))
        for index in range(page._permission.count())
    ]

    assert listed == [("Read only", "read_only"), ("Read / Write", "read_write")]


def test_the_window_carries_no_terminal_warning_of_any_kind(wired) -> None:
    """The warning line is gone, and nothing replaced it."""
    from PySide6.QtWidgets import QLabel

    wired.controller.on_agents_requested()
    page = wired.controller.agents_page

    assert not hasattr(page, "_warning")
    texts = " ".join(
        label.text().lower() for label in page.findChildren(QLabel)
    ) + " ".join(
        str(widget.toolTip()).lower() for widget in page.findChildren(QWidget)
    )
    assert "does not sandbox" not in texts
    assert "run as you" not in texts


# ── a running turn ───────────────────────────────────────────────────────────


def test_a_turn_freezes_every_change_but_keeps_the_page_readable(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page
    page.select_agent(agent_id)

    wired.controller.set_execution_active(True)

    assert page.mutations_enabled() is False
    assert page.isVisible() is True
    assert page.visible_agent_ids()["project"] == (agent_id,)
    assert page._new_project_btn.isEnabled() is False
    assert page._new_personal_btn.isEnabled() is False
    assert page._save_btn.isEnabled() is False
    assert page._delete_btn.isEnabled() is False
    assert page._permission.isEnabled() is False
    assert not (
        page._items[f"project:{agent_id}"].flags()
        & Qt.ItemFlag.ItemIsUserCheckable
    )
    assert page._instructions.isReadOnly() is True

    wired.controller.set_execution_active(False)

    assert page.mutations_enabled() is True
    assert page._save_btn.isEnabled() is True
    assert (
        page._items[f"project:{agent_id}"].flags()
        & Qt.ItemFlag.ItemIsUserCheckable
    )


def test_mutations_arriving_during_a_turn_are_refused(wired) -> None:
    agent_id = _seed(wired.store, AgentScope.PROJECT, "Reviewer")
    wired.controller.on_agents_requested()
    page = wired.controller.agents_page

    wired.controller.set_execution_active(True)
    page.create_requested.emit("project")
    page.availability_changed.emit(agent_id, True)
    page.permission_changed.emit(agent_id, AgentPermission.READ_WRITE.value)
    page.delete_requested.emit("project", agent_id)

    assert [row.agent_id for row in wired.store.list_summaries()] == [agent_id]
    assert wired.state.available_ids() == ()
    assert wired.state.permission(agent_id) is AgentPermission.READ_ONLY
