"""The user-facing Skills manager: inventory, selection, and lifecycle.

Covers the controller/window split for the first shipped slice — grouped and
searchable Project/Personal/Bundled inventory, inspection, "Use in next
message" handoff into the composer, enable/disable, and project/personal
uninstall. SkillLibrary stays the only discovery and lifecycle backend here:
these tests assert the manager *reports* its judgements (precedence,
disabled state, workspace markers, invalid entries) rather than making its
own.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from aura.gui.input_panel import InputPanel
from aura.gui.skills_manager import SkillsManagerController
from aura.skills.library import SkillLibrary


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_skill(
    directory: Path,
    name: str,
    *,
    description: str = "A focused procedure.",
    extra_frontmatter: str = "",
    body: str = "# Procedure\n\nDo the careful thing.\n",
) -> Path:
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    front = f"name: {name}\ndescription: {description}\n{extra_frontmatter}".rstrip()
    (folder / "SKILL.md").write_text(f"---\n{front}\n---\n{body}", encoding="utf-8")
    return folder


class _Dialogs:
    """Stand-in for the controller's QMessageBox use, captured not shown."""

    StandardButton = QMessageBox.StandardButton

    def __init__(self, *, confirm: bool = True) -> None:
        self.confirm = confirm
        self.questions: list[str] = []
        self.warnings: list[str] = []

    def question(self, _parent, _title, text, *_args, **_kwargs):
        self.questions.append(text)
        return (
            QMessageBox.StandardButton.Yes if self.confirm else QMessageBox.StandardButton.No
        )

    def warning(self, _parent, title, text, *_args, **_kwargs) -> None:
        self.warnings.append(f"{title}: {text}")


class _Manager:
    """One assembled manager: real composer, real library, fake dialogs."""

    def __init__(self, tmp_path: Path, monkeypatch, *, confirm: bool = True) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.project_dir = self.workspace / ".aura" / "skills" / "authored"
        self.personal_dir = tmp_path / "personal"
        self.bundled_dir = tmp_path / "bundled"
        for directory in (self.project_dir, self.personal_dir, self.bundled_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.dialogs = _Dialogs(confirm=confirm)
        monkeypatch.setattr(
            "aura.gui.skills_manager.controller.QMessageBox", self.dialogs
        )
        self.input = InputPanel(self.workspace)
        self.controller = SkillsManagerController(
            input_panel=self.input,
            workspace_root=self.workspace,
            library_factory=self._library,
        )

    def _library(self, workspace: Path) -> SkillLibrary:
        return SkillLibrary(
            workspace,
            personal_dir=self.personal_dir,
            bundled_dir=self.bundled_dir,
        )

    def project_library(self) -> SkillLibrary:
        return self._library(self.workspace)

    def open(self):
        self.controller.open_manager()
        return self.controller.window

    def row(self, install_id: str):
        return self.controller._rows[install_id]

    def chip_ids(self) -> tuple[str, ...]:
        return tuple(skill.install_id for skill in self.input.selected_skills())


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch, qapp) -> _Manager:
    return _Manager(tmp_path, monkeypatch)


# ── inventory, grouping, and search ──────────────────────────────────────────


def test_inventory_is_grouped_by_scope(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "alpha-one", description="Handles widgets.")
    _write_skill(manager.personal_dir, "beta-two", description="Handles pipelines.")
    _write_skill(manager.bundled_dir, "gamma-three", description="Handles rendering.")

    window = manager.open()

    assert window.visible_row_ids() == {
        "project": ("project:alpha-one",),
        "personal": ("personal:beta-two",),
        "bundled": ("bundled:gamma-three",),
    }


def test_search_filters_across_fields_and_keeps_grouping(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "alpha-one", description="Handles widgets.")
    _write_skill(manager.personal_dir, "beta-two", description="Handles pipelines.")
    _write_skill(manager.bundled_dir, "gamma-three", description="Handles widgets too.")
    window = manager.open()

    window.set_search_text("pipelines")
    assert window.visible_row_ids() == {
        "project": (),
        "personal": ("personal:beta-two",),
        "bundled": (),
    }

    window.set_search_text("widgets")
    assert window.visible_row_ids() == {
        "project": ("project:alpha-one",),
        "personal": (),
        "bundled": ("bundled:gamma-three",),
    }

    window.set_search_text("bundled")
    assert window.visible_row_ids()["bundled"] == ("bundled:gamma-three",)

    manager.project_library().set_enabled("project:alpha-one", False)
    manager.controller.refresh()
    window.set_search_text("disabled")
    assert window.visible_row_ids() == {
        "project": ("project:alpha-one",),
        "personal": (),
        "bundled": (),
    }


def test_invalid_and_shadowed_entries_stay_visible(manager: _Manager) -> None:
    broken = manager.project_dir / "broken-one"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: broken-one\n", encoding="utf-8")
    _write_skill(manager.project_dir, "shared", description="Project wins.")
    _write_skill(manager.personal_dir, "shared", description="Personal loses.")
    window = manager.open()

    assert window.visible_row_ids()["project"] == ("project:broken-one", "project:shared")
    assert window.visible_row_ids()["personal"] == ("personal:shared",)
    assert manager.row("project:broken-one").status_text == "Invalid"
    assert manager.row("project:broken-one").valid is False
    assert "Shadowed" in manager.row("personal:shared").status_text
    assert manager.row("project:shared").status_text == "Enabled"


def test_selectable_state_matches_the_effective_set(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "usable")
    _write_skill(manager.project_dir, "turned-off")
    _write_skill(manager.personal_dir, "shared")
    _write_skill(manager.project_dir, "shared")
    _write_skill(
        manager.bundled_dir,
        "node-only",
        extra_frontmatter="workspace_markers:\n  - package.json\n",
    )
    manager.project_library().set_enabled("project:turned-off", False)
    manager.open()

    effective, _diagnostics = manager.project_library().discover_effective_skills()
    effective_ids = {skill.install_id for skill in effective}
    usable_ids = {row.install_id for row in manager.controller._rows.values() if row.usable}

    assert usable_ids == effective_ids
    assert manager.row("project:usable").usable is True
    assert manager.row("project:turned-off").usable is False
    assert manager.row("personal:shared").usable is False
    assert manager.row("bundled:node-only").usable is False
    assert manager.row("bundled:node-only").status_text == "Not available in this workspace"


def test_reopening_reuses_one_window_and_refreshes_inventory(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "first")
    window = manager.open()
    assert window.visible_row_ids()["project"] == ("project:first",)

    _write_skill(manager.project_dir, "second")
    reopened = manager.open()

    assert reopened is window
    assert reopened.isVisible()
    assert window.visible_row_ids()["project"] == ("project:first", "project:second")


# ── detail pane ──────────────────────────────────────────────────────────────


def test_details_show_metadata_and_never_a_filesystem_path(manager: _Manager) -> None:
    folder = _write_skill(
        manager.project_dir,
        "documented",
        description="Explains the widget pipeline.",
        extra_frontmatter=(
            "model: careful-model\n"
            "task_kinds:\n  - debug\n"
            "path_globs:\n  - src/**/*.py\n"
            "triggers:\n  - widget\n"
        ),
    )
    (folder / "references").mkdir()
    (folder / "references" / "notes.md").write_text("notes", encoding="utf-8")
    broken = manager.project_dir / "broken-two"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\nname: broken-two\n", encoding="utf-8")
    window = manager.open()

    window.select_row("project:documented")
    text = window.details_text()
    assert "documented" in text
    assert "Project" in text and "Enabled" in text
    assert "Explains the widget pipeline." in text
    assert "Model: careful-model" in text
    assert "Task kinds: debug" in text
    assert "Paths: src/**/*.py" in text
    assert "Triggers: widget" in text
    assert "Resources: references" in text

    window.select_row("project:broken-two")
    broken_text = window.details_text()
    assert "Invalid" in broken_text
    assert "Diagnostics:" in broken_text

    for rendered in (text, broken_text):
        assert str(manager.workspace) not in rendered
        assert str(manager.project_dir) not in rendered
        assert ".aura" not in rendered


# ── composer handoff ─────────────────────────────────────────────────────────


def test_selecting_a_skill_adds_exactly_one_chip_and_dedupes(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "first")
    _write_skill(manager.personal_dir, "second")
    window = manager.open()

    window.select_row("project:first")
    window._use_btn.click()
    window._use_btn.click()  # already added: the button is disabled and inert

    assert manager.chip_ids() == ("project:first",)
    assert manager.row("project:first").already_selected is True
    assert window._use_btn.isEnabled() is False
    assert window._use_btn.text() == "Added to next message"
    assert window.isVisible()

    window.select_row("personal:second")
    window._use_btn.click()

    assert manager.chip_ids() == ("project:first", "personal:second")


def test_unselectable_rows_cannot_be_added(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "turned-off")
    manager.project_library().set_enabled("project:turned-off", False)
    window = manager.open()

    window.select_row("project:turned-off")
    window._use_btn.click()
    manager.controller._on_use_requested("project:turned-off")

    assert window._use_btn.isEnabled() is False
    assert manager.chip_ids() == ()


def test_removing_a_chip_updates_manager_state(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "first")
    window = manager.open()
    window.select_row("project:first")
    window._use_btn.click()
    assert manager.row("project:first").already_selected is True

    manager.input.remove_selected_skill("project:first")

    assert manager.chip_ids() == ()
    assert manager.row("project:first").already_selected is False
    assert window._use_btn.isEnabled() is True


# ── enable / disable ─────────────────────────────────────────────────────────


def test_enable_disable_refreshes_availability(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "toggled")
    window = manager.open()
    window.select_row("project:toggled")
    assert window._enable_btn.text() == "Disable"

    window._enable_btn.click()

    assert manager.row("project:toggled").enabled is False
    assert manager.row("project:toggled").usable is False
    assert manager.row("project:toggled").status_text == "Disabled"
    assert window._enable_btn.text() == "Enable"
    assert window._use_btn.isEnabled() is False

    window._enable_btn.click()

    assert manager.row("project:toggled").enabled is True
    assert manager.row("project:toggled").usable is True
    assert window._use_btn.isEnabled() is True


def test_disabling_removes_only_that_skills_unsent_chip(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "kept")
    _write_skill(manager.project_dir, "dropped")
    window = manager.open()
    for install_id in ("project:kept", "project:dropped"):
        window.select_row(install_id)
        window._use_btn.click()
    assert manager.chip_ids() == ("project:kept", "project:dropped")

    window.select_row("project:dropped")
    window._enable_btn.click()

    assert manager.chip_ids() == ("project:kept",)


# ── uninstall ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["project", "personal"])
def test_uninstall_after_confirmation_removes_skill_and_its_chip(
    manager: _Manager, scope: str
) -> None:
    directory = manager.project_dir if scope == "project" else manager.personal_dir
    folder = _write_skill(directory, "removable")
    _write_skill(manager.project_dir, "kept")
    install_id = f"{scope}:removable"
    window = manager.open()
    window.select_row(install_id)
    window._use_btn.click()
    window.select_row("project:kept")
    window._use_btn.click()

    window.select_row(install_id)
    window._uninstall_btn.click()

    assert manager.dialogs.questions
    assert "removable" in manager.dialogs.questions[0]
    assert scope in manager.dialogs.questions[0]
    assert not folder.exists()
    assert install_id not in manager.controller._rows
    assert manager.chip_ids() == ("project:kept",)


def test_declined_uninstall_changes_nothing(tmp_path: Path, monkeypatch, qapp) -> None:
    manager = _Manager(tmp_path, monkeypatch, confirm=False)
    folder = _write_skill(manager.project_dir, "kept-skill")
    window = manager.open()

    window.select_row("project:kept-skill")
    window._uninstall_btn.click()

    assert manager.dialogs.questions
    assert folder.exists()
    assert "project:kept-skill" in manager.controller._rows


def test_broken_project_entry_stays_uninstallable(manager: _Manager) -> None:
    broken = manager.project_dir / "broken-three"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: broken-three\n", encoding="utf-8")
    window = manager.open()

    window.select_row("project:broken-three")
    assert window._uninstall_btn.isHidden() is False
    window._uninstall_btn.click()

    assert not broken.exists()
    assert "project:broken-three" not in manager.controller._rows


def test_bundled_uninstall_is_unavailable_and_cannot_be_invoked(manager: _Manager) -> None:
    folder = _write_skill(manager.bundled_dir, "packaged")
    window = manager.open()

    window.select_row("bundled:packaged")

    assert manager.row("bundled:packaged").can_uninstall is False
    assert window._uninstall_btn.isHidden() is True
    window._uninstall_btn.click()
    manager.controller._on_uninstall_requested("bundled:packaged")

    assert folder.exists()
    assert manager.dialogs.questions == []


def test_uninstalling_a_winner_reveals_the_lower_precedence_skill(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "shared", description="Project wins.")
    _write_skill(manager.personal_dir, "shared", description="Personal loses.")
    window = manager.open()
    assert manager.row("personal:shared").usable is False

    window.select_row("project:shared")
    window._uninstall_btn.click()

    assert manager.row("personal:shared").usable is True
    assert manager.row("personal:shared").status_text == "Enabled"


class _RaisingLibrary:
    """Delegating library whose uninstall fails, for local-error coverage."""

    def __init__(self, inner: SkillLibrary, failure: Exception) -> None:
        self._inner = inner
        self._failure = failure

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def uninstall(self, _installed_id: str) -> None:
        raise self._failure


def test_lifecycle_errors_stay_a_local_dialog(manager: _Manager) -> None:
    folder = _write_skill(manager.project_dir, "explodes")
    window = manager.open()
    failure = OSError(f"[Errno 13] Permission denied: '{folder}'")
    manager.controller._library_factory = lambda workspace: _RaisingLibrary(
        SkillLibrary(
            workspace,
            personal_dir=manager.personal_dir,
            bundled_dir=manager.bundled_dir,
        ),
        failure,
    )

    window.select_row("project:explodes")
    window._uninstall_btn.click()

    assert manager.dialogs.warnings
    message = manager.dialogs.warnings[0]
    assert "explodes" in message
    assert "Permission denied" in message
    # A local failure never leaks where Aura keeps skills on disk.
    assert str(manager.project_dir) not in message
    assert folder.exists()


# ── production-turn safety and workspace lifecycle ───────────────────────────


def test_mutations_are_disabled_during_an_active_turn(manager: _Manager) -> None:
    folder = _write_skill(manager.project_dir, "active-turn")
    window = manager.open()
    window.select_row("project:active-turn")

    manager.controller.set_execution_active(True)

    assert window._enable_btn.isEnabled() is False
    assert window._uninstall_btn.isEnabled() is False
    # Browsing and selecting for the next message stay available.
    assert window._use_btn.isEnabled() is True
    assert window.visible_row_ids()["project"] == ("project:active-turn",)

    manager.controller._on_enable_toggle_requested("project:active-turn", False)
    manager.controller._on_uninstall_requested("project:active-turn")

    assert manager.row("project:active-turn").enabled is True
    assert folder.exists()
    assert manager.dialogs.questions == []
    assert len(manager.dialogs.warnings) == 2

    manager.controller.set_execution_active(False)
    assert window._enable_btn.isEnabled() is True
    assert window._uninstall_btn.isEnabled() is True


def test_selection_during_an_active_turn_still_reaches_the_composer(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "queued-skill")
    window = manager.open()
    manager.controller.set_execution_active(True)

    window.select_row("project:queued-skill")
    window._use_btn.click()

    assert manager.chip_ids() == ("project:queued-skill",)


def test_workspace_change_cannot_retain_stale_project_inventory(manager: _Manager) -> None:
    _write_skill(manager.project_dir, "first-workspace")
    _write_skill(manager.personal_dir, "everywhere")
    window = manager.open()
    window.select_row("project:first-workspace")
    window._use_btn.click()
    assert manager.chip_ids() == ("project:first-workspace",)

    other = manager.workspace.parent / "other-workspace"
    other_project_dir = other / ".aura" / "skills" / "authored"
    other_project_dir.mkdir(parents=True)
    _write_skill(other_project_dir, "second-workspace")
    manager.input.set_workspace_root(other)
    manager.controller.set_workspace_root(other)

    assert window.visible_row_ids()["project"] == ("project:second-workspace",)
    assert window.visible_row_ids()["personal"] == ("personal:everywhere",)
    assert "project:first-workspace" not in manager.controller._rows
    assert manager.chip_ids() == ()
