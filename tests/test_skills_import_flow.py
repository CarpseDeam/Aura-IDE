"""The user-facing skill import flow: source, review, install, cleanup.

Covers the second Skills-manager slice — "Import Folder/ZIP" and "Install
from GitHub" — end to end through the real controllers, the real
SkillImporter, and the real review dialog. SkillImporter stays the only
backend: these tests assert the GUI *asks* it and *reports* it, never that
the GUI re-derives conflicts, validity, or installation.

Nothing here touches the network. The GitHub path runs through an injected
fake fetcher, and every import runs on the real background worker, pumped by
the test's own event loop.
"""
from __future__ import annotations

import shutil
import threading
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from aura.gui.input_panel import InputPanel  # noqa: E402
from aura.gui.skills_manager import SkillsManagerController, import_worker  # noqa: E402
from aura.gui.skills_manager.import_dialogs import (  # noqa: E402
    ImportReviewDialog,
    InstallScopeDialog,
)
from aura.gui.skills_manager.import_models import (  # noqa: E402
    SOURCE_FOLDER,
    SOURCE_ZIP,
    ImportDecision,
)
from aura.gui.skills_manager.window import IMPORT_GITHUB, IMPORT_LOCAL  # noqa: E402
from aura.skills.identity import InstallScope  # noqa: E402
from aura.skills.importer import SkillImporter  # noqa: E402
from aura.skills.library import SkillLibrary  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_lingering_threads():
    yield
    import_worker._LINGERING_THREADS.clear()


def _pump(qapp, predicate, timeout_s: float = 10.0) -> bool:
    """Spin the Qt event loop until *predicate* holds or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.005)
    return predicate()


def _write_skill(
    directory: Path,
    name: str,
    *,
    description: str = "A focused procedure.",
    body: str = "# Procedure\n\nDo the careful thing.\n",
    frontmatter: str | None = None,
) -> Path:
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    front = frontmatter if frontmatter is not None else f"name: {name}\ndescription: {description}\n"
    (folder / "SKILL.md").write_text(f"---\n{front}---\n{body}", encoding="utf-8")
    return folder


def _zip_of(folder: Path, destination: Path) -> Path:
    with zipfile.ZipFile(destination, "w") as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, f"{folder.name}/{path.relative_to(folder).as_posix()}")
    return destination


class _Dialogs:
    """Stand-in for the manager controller's QMessageBox use."""

    StandardButton = QMessageBox.StandardButton

    def __init__(self, *, confirm: bool = True) -> None:
        self.confirm = confirm
        self.questions: list[str] = []
        self.warnings: list[str] = []

    def question(self, _parent, _title, text, *_args, **_kwargs):
        self.questions.append(text)
        return QMessageBox.StandardButton.Yes if self.confirm else QMessageBox.StandardButton.No

    def warning(self, _parent, title, text, *_args, **_kwargs) -> None:
        self.warnings.append(f"{title}: {text}")


class _Prompts:
    """Every import question answered by the test instead of a native dialog."""

    def __init__(self) -> None:
        self.kind = SOURCE_FOLDER
        self.folder = ""
        self.archive = ""
        self.url = ""
        self.scope: InstallScope | None = InstallScope.PROJECT
        self.decision = ImportDecision.INSTALL
        self.on_review = None
        self.views: list = []
        self.errors: list[str] = []
        self.closed_reviews = 0
        self.asked: list[str] = []

    def ask_local_source_kind(self, _parent) -> str:
        self.asked.append("kind")
        return self.kind

    def ask_folder(self, _parent) -> str:
        self.asked.append("folder")
        return self.folder

    def ask_zip(self, _parent) -> str:
        self.asked.append("zip")
        return self.archive

    def ask_github_url(self, _parent) -> str:
        self.asked.append("url")
        return self.url

    def ask_scope(self, _parent):
        self.asked.append("scope")
        return self.scope

    def review(self, _parent, view):
        self.asked.append("review")
        self.views.append(view)
        if self.on_review is not None:
            self.on_review(view)
        return self.decision

    def close_review(self) -> None:
        self.closed_reviews += 1

    def show_error(self, _parent, title: str, message: str) -> None:
        self.errors.append(f"{title}: {message}")


class _FakeGitHubFetcher:
    """Stands in for GitHubSkillFetcher. No network, ever."""

    def __init__(self, skill_dir: Path) -> None:
        self._skill_dir = skill_dir
        self.targets: list = []

    def fetch(self, target, staging_root: Path) -> Path:
        self.targets.append(target)
        destination = Path(staging_root) / "github-download" / self._skill_dir.name
        shutil.copytree(self._skill_dir, destination)
        return destination


class _RecordingImporter(SkillImporter):
    """A real importer that records its staging roots and can be held open."""

    def __init__(self, library, *, github_fetcher=None, roots=None, gate=None) -> None:
        super().__init__(library, github_fetcher=github_fetcher)
        self._roots = roots if roots is not None else []
        self._gate = gate

    def _new_staging_root(self) -> Path:
        root = super()._new_staging_root()
        self._roots.append(root)
        return root

    def _finish_preview(self, staging_root, staged_dir, destination_scope):
        if self._gate is not None:
            self._gate.wait(timeout=10)
        return super()._finish_preview(staging_root, staged_dir, destination_scope)


class _Harness:
    """One assembled manager: real composer, real library, real importer."""

    def __init__(self, tmp_path: Path, monkeypatch, qapp) -> None:
        self.qapp = qapp
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.other_workspace = tmp_path / "other-workspace"
        for root in (self.workspace, self.other_workspace):
            root.mkdir(parents=True, exist_ok=True)
        self.project_dir = self.workspace / ".aura" / "skills" / "authored"
        self.personal_dir = tmp_path / "personal"
        self.bundled_dir = tmp_path / "bundled"
        self.sources = tmp_path / "sources"
        for directory in (self.project_dir, self.personal_dir, self.bundled_dir, self.sources):
            directory.mkdir(parents=True, exist_ok=True)

        self.staging_roots: list[Path] = []
        self.gate: threading.Event | None = None
        self.github_fetcher = None

        self.dialogs = _Dialogs()
        monkeypatch.setattr("aura.gui.skills_manager.controller.QMessageBox", self.dialogs)
        self.prompts = _Prompts()
        self.input = InputPanel(self.workspace)
        self.controller = SkillsManagerController(
            input_panel=self.input,
            workspace_root=self.workspace,
            library_factory=self._library,
            importer_factory=self._importer,
            import_prompts=self.prompts,
        )
        self.installed: list[str] = []
        self.controller._imports.import_succeeded.connect(self.installed.append)

    # ---- assembly ----------------------------------------------------------

    def _library(self, workspace: Path) -> SkillLibrary:
        return SkillLibrary(workspace, personal_dir=self.personal_dir, bundled_dir=self.bundled_dir)

    def _importer(self, library: SkillLibrary) -> SkillImporter:
        return _RecordingImporter(
            library,
            github_fetcher=self.github_fetcher,
            roots=self.staging_roots,
            gate=self.gate,
        )

    # ---- driving -----------------------------------------------------------

    def open(self):
        self.controller.open_manager()
        return self.controller.window

    def request(self, kind: str = IMPORT_LOCAL) -> None:
        """Ask for an import the way the window does, signal and all."""
        self.controller.window.import_requested.emit(kind)

    def run(self, kind: str = IMPORT_LOCAL, timeout_s: float = 10.0) -> bool:
        self.request(kind)
        return _pump(self.qapp, lambda: not self.controller._imports.is_active(), timeout_s)

    def import_folder(self, folder: Path, *, scope=InstallScope.PROJECT, decision=None) -> bool:
        self.prompts.kind = SOURCE_FOLDER
        self.prompts.folder = str(folder)
        self.prompts.scope = scope
        if decision is not None:
            self.prompts.decision = decision
        return self.run(IMPORT_LOCAL)

    def import_zip(self, archive: Path, *, scope=InstallScope.PERSONAL, decision=None) -> bool:
        self.prompts.kind = SOURCE_ZIP
        self.prompts.archive = str(archive)
        self.prompts.scope = scope
        if decision is not None:
            self.prompts.decision = decision
        return self.run(IMPORT_LOCAL)

    def import_github(self, url: str, *, scope=InstallScope.PROJECT, decision=None) -> bool:
        self.prompts.url = url
        self.prompts.scope = scope
        if decision is not None:
            self.prompts.decision = decision
        return self.run(IMPORT_GITHUB)

    # ---- inspection --------------------------------------------------------

    def install_ids(self) -> set[str]:
        return set(self.controller._rows)

    def chip_ids(self) -> tuple[str, ...]:
        return tuple(skill.install_id for skill in self.input.selected_skills())

    def staging_survivors(self) -> list[Path]:
        return [root for root in self.staging_roots if root.exists()]

    def source_skill(self, name: str, **kwargs) -> Path:
        return _write_skill(self.sources, name, **kwargs)


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch, qapp) -> _Harness:
    manager = _Harness(tmp_path, monkeypatch, qapp)
    yield manager
    manager.controller.shutdown()


# ── the two happy paths ──────────────────────────────────────────────────────


def test_folder_preview_and_install_into_project(harness: _Harness) -> None:
    source = harness.source_skill("folder-skill", description="Handles widgets.")
    harness.open()

    assert harness.import_folder(source, scope=InstallScope.PROJECT)

    assert harness.installed == ["project:folder-skill"]
    assert (harness.project_dir / "folder-skill" / "SKILL.md").is_file()
    assert "project:folder-skill" in harness.install_ids()
    assert harness.staging_survivors() == []
    assert harness.prompts.errors == []


def test_zip_preview_and_install_into_personal(harness: _Harness) -> None:
    source = harness.source_skill("zipped-skill", description="Handles pipelines.")
    archive = _zip_of(source, harness.tmp_path / "zipped-skill.zip")
    harness.open()

    assert harness.import_zip(archive, scope=InstallScope.PERSONAL)

    assert harness.installed == ["personal:zipped-skill"]
    assert (harness.personal_dir / "zipped-skill" / "SKILL.md").is_file()
    view = harness.prompts.views[0]
    assert view.destination_label == "Personal"
    assert view.source_label == "zipped-skill.zip"
    assert harness.staging_survivors() == []


def test_github_flow_uses_the_injected_fetcher_and_never_the_network(harness: _Harness) -> None:
    remote = _write_skill(harness.tmp_path / "fake-repo", "remote-skill", description="From a repo.")
    harness.github_fetcher = _FakeGitHubFetcher(remote)
    harness.open()

    url = "https://github.com/acme/widgets/tree/main/remote-skill"
    assert harness.import_github(url, scope=InstallScope.PROJECT)

    assert harness.installed == ["project:remote-skill"]
    assert harness.github_fetcher.targets[0].owner == "acme"
    assert harness.github_fetcher.targets[0].subpath == "remote-skill"
    assert harness.prompts.views[0].source_label == url
    assert harness.staging_survivors() == []


def test_a_bad_github_url_fails_locally_without_staging(harness: _Harness) -> None:
    harness.github_fetcher = _FakeGitHubFetcher(harness.source_skill("unused"))
    harness.open()

    assert harness.import_github("https://example.com/not-github")

    assert harness.installed == []
    assert harness.prompts.errors and "not a supported GitHub URL" in harness.prompts.errors[0]
    assert harness.staging_survivors() == []


# ── destination scope ────────────────────────────────────────────────────────


def test_scope_dialog_offers_project_and_personal_only(qapp) -> None:
    dialog = InstallScopeDialog()
    try:
        assert dialog.offered_scopes() == (InstallScope.PROJECT, InstallScope.PERSONAL)
        assert InstallScope.BUNDLED not in dialog.offered_scopes()
        assert dialog.selected_scope() is InstallScope.PROJECT
    finally:
        dialog.deleteLater()


def test_the_destination_question_is_asked_before_anything_is_staged(harness: _Harness) -> None:
    harness.open()
    harness.prompts.scope = None  # the user backed out of the destination
    harness.prompts.kind = SOURCE_FOLDER
    harness.prompts.folder = str(harness.source_skill("never-staged"))

    assert harness.run(IMPORT_LOCAL)

    assert harness.prompts.asked == ["kind", "folder", "scope"]
    assert harness.staging_roots == []
    assert harness.installed == []


# ── what the review shows ────────────────────────────────────────────────────


def _rendered(view, qapp) -> str:
    dialog = ImportReviewDialog(view)
    try:
        return dialog.rendered_text()
    finally:
        dialog.deleteLater()


def test_review_renders_description_counts_resources_and_scripts(harness: _Harness, qapp) -> None:
    source = harness.source_skill("rich-skill", description="Summarises release notes.")
    (source / "references").mkdir()
    (source / "references" / "notes.md").write_text("notes", encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "setup.sh").write_text("echo hi", encoding="utf-8")
    harness.open()
    harness.prompts.decision = ImportDecision.CANCEL

    assert harness.import_folder(source)

    view = harness.prompts.views[0]
    assert view.description == "Summarises release notes."
    assert view.file_count == 3
    # The importer's own order, reported rather than re-derived.
    assert view.resource_dirs_text == "scripts, references"
    assert view.has_scripts is True

    text = _rendered(view, qapp)
    assert "Summarises release notes." in text
    assert "Files: 3" in text
    assert "Resource folders: scripts, references" in text
    assert "Scripts or executable files: Yes" in text
    assert "Destination: Project — Available in this project" in text
    assert "Already installed with this name: No" in text
    # The capability boundary is restated where the decision is made.
    for capability in ("shell", "network", "file-mutation", "external-read", "script-execution"):
        assert capability in text
    assert "does not run anything in this skill" in text


def test_review_reports_no_resource_folders_and_no_scripts(harness: _Harness, qapp) -> None:
    source = harness.source_skill("plain-skill")
    harness.open()
    harness.prompts.decision = ImportDecision.CANCEL

    assert harness.import_folder(source)

    view = harness.prompts.views[0]
    text = _rendered(view, qapp)
    assert "Resource folders: None" in text
    assert "Scripts or executable files: No" in text
    assert "Validation: no problems found." in text


def test_review_lists_validation_warnings_with_severity(harness: _Harness, qapp) -> None:
    source = harness.source_skill(
        "warned-skill",
        frontmatter="name: warned-skill\n",
        body="# Warned\n\nA body that stands in for the missing description.\n",
    )
    harness.open()
    harness.prompts.decision = ImportDecision.CANCEL

    assert harness.import_folder(source)

    view = harness.prompts.views[0]
    assert any(line.startswith("warning: missing_description") for line in view.diagnostics)
    assert view.installable is True
    assert "warning: missing_description" in _rendered(view, qapp)


# ── an invalid preview is reviewable but not installable ─────────────────────


def test_an_invalid_preview_cannot_be_installed(harness: _Harness, qapp) -> None:
    broken = harness.sources / "broken-skill"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "notes.txt").write_text("no SKILL.md here", encoding="utf-8")
    harness.open()
    # Even if something answered "install", an invalid preview never installs.
    harness.prompts.decision = ImportDecision.INSTALL

    assert harness.import_folder(broken)

    view = harness.prompts.views[0]
    assert view.installable is False
    assert any("missing_skill_md" in line for line in view.diagnostics)

    dialog = ImportReviewDialog(view)
    try:
        assert dialog.install_action_text() == ""
        assert "cannot be installed" in dialog.rendered_text()
    finally:
        dialog.deleteLater()

    assert harness.installed == []
    assert not (harness.project_dir / "broken-skill").exists()
    assert harness.staging_survivors() == []


# ── conflict requires an explicit replacement ────────────────────────────────


def _install_existing(harness: _Harness, name: str, marker: str) -> None:
    _write_skill(harness.project_dir, name, description="The installed one.", body=f"# {marker}\n")


def test_a_conflicting_preview_offers_replacement_and_nothing_else(harness: _Harness, qapp) -> None:
    _install_existing(harness, "shared-name", "ORIGINAL")
    source = harness.source_skill("shared-name", description="The incoming one.")
    harness.open()
    harness.prompts.decision = ImportDecision.CANCEL

    assert harness.import_folder(source)

    view = harness.prompts.views[0]
    assert view.conflict is True
    assert view.decision is ImportDecision.REPLACE

    dialog = ImportReviewDialog(view)
    try:
        assert dialog.install_action_text() == "Replace existing skill"
        assert dialog.decision() is ImportDecision.CANCEL  # nothing clicked yet
        assert "Already installed with this name: Yes" in dialog.rendered_text()
    finally:
        dialog.deleteLater()

    assert "ORIGINAL" in (harness.project_dir / "shared-name" / "SKILL.md").read_text(encoding="utf-8")
    assert harness.installed == []
    assert harness.staging_survivors() == []


def test_cancelling_a_conflict_leaves_the_installed_skill_alone(harness: _Harness) -> None:
    _install_existing(harness, "keep-me", "ORIGINAL")
    source = harness.source_skill("keep-me", description="The incoming one.")
    harness.open()

    assert harness.import_folder(source, decision=ImportDecision.CANCEL)

    assert "ORIGINAL" in (harness.project_dir / "keep-me" / "SKILL.md").read_text(encoding="utf-8")
    assert harness.installed == []


def test_explicit_replacement_installs_over_the_existing_skill(harness: _Harness) -> None:
    _install_existing(harness, "replace-me", "ORIGINAL")
    source = harness.source_skill("replace-me", description="The incoming one.")
    (source / "SKILL.md").write_text(
        "---\nname: replace-me\ndescription: The incoming one.\n---\n# REPLACED\n",
        encoding="utf-8",
    )
    harness.open()

    assert harness.import_folder(source, decision=ImportDecision.REPLACE)

    installed = (harness.project_dir / "replace-me" / "SKILL.md").read_text(encoding="utf-8")
    assert "REPLACED" in installed
    assert harness.installed == ["project:replace-me"]
    assert harness.staging_survivors() == []


def test_a_plain_install_over_a_conflict_is_refused_not_silently_replaced(harness: _Harness) -> None:
    """Replacement is never inferred: only the Replace action passes replace=True."""
    _install_existing(harness, "guarded", "ORIGINAL")
    source = harness.source_skill("guarded", description="The incoming one.")
    harness.open()

    assert harness.import_folder(source, decision=ImportDecision.INSTALL)

    assert harness.installed == []
    assert "ORIGINAL" in (harness.project_dir / "guarded" / "SKILL.md").read_text(encoding="utf-8")
    assert harness.prompts.errors
    assert "explicit replacement is required" in harness.prompts.errors[0]
    assert harness.staging_survivors() == []


def test_a_conflict_appearing_after_the_preview_is_not_replaced_automatically(harness: _Harness) -> None:
    """The preview said "no conflict"; the destination filled in afterwards."""
    source = harness.source_skill("racy", description="The incoming one.")
    harness.open()

    def _occupy(_view) -> None:
        _install_existing(harness, "racy", "APPEARED")

    harness.prompts.on_review = _occupy
    harness.prompts.decision = ImportDecision.INSTALL

    assert harness.import_folder(source)

    # One preview, one refusal, no second attempt and no replacement.
    assert len(harness.prompts.views) == 1
    assert harness.prompts.views[0].conflict is False
    assert harness.installed == []
    assert "APPEARED" in (harness.project_dir / "racy" / "SKILL.md").read_text(encoding="utf-8")
    assert harness.prompts.errors
    assert "Run the import again" in harness.prompts.errors[0]
    assert harness.staging_survivors() == []


# ── staging never outlives its session ───────────────────────────────────────


def test_cancelling_the_review_cleans_up_staging(harness: _Harness) -> None:
    source = harness.source_skill("cancelled")
    harness.open()

    assert harness.import_folder(source, decision=ImportDecision.CANCEL)

    assert harness.staging_roots and harness.staging_survivors() == []
    assert harness.installed == []


def test_a_failed_install_cleans_up_staging(harness: _Harness, monkeypatch) -> None:
    source = harness.source_skill("doomed")
    harness.open()

    def _fail(self, *_args, **_kwargs):
        raise OSError("disk is having a day")

    monkeypatch.setattr(_RecordingImporter, "install", _fail, raising=False)

    assert harness.import_folder(source)

    assert harness.installed == []
    assert harness.staging_roots and harness.staging_survivors() == []
    assert harness.prompts.errors


def test_a_staging_failure_cleans_up_and_stays_local(harness: _Harness) -> None:
    harness.open()

    assert harness.import_folder(harness.sources / "does-not-exist")

    assert harness.installed == []
    assert harness.staging_survivors() == []
    assert harness.prompts.errors
    # No chat message, no history entry, no model call — just this dialog.
    assert harness.dialogs.warnings == []


def test_a_late_preview_is_cleaned_without_ever_being_shown(harness: _Harness, qapp) -> None:
    harness.gate = threading.Event()
    source = harness.source_skill("late-arrival")
    harness.open()
    harness.prompts.kind = SOURCE_FOLDER
    harness.prompts.folder = str(source)
    harness.request(IMPORT_LOCAL)

    assert _pump(qapp, lambda: bool(harness.staging_roots)), "staging never started"
    # The session is abandoned while its preview is still being built.
    harness.controller.set_workspace_root(harness.other_workspace)
    harness.gate.set()

    assert _pump(qapp, lambda: harness.staging_survivors() == [])
    assert harness.prompts.views == []
    assert harness.installed == []
    assert not (harness.project_dir / "late-arrival").exists()


def test_shutdown_drops_staged_content_and_never_destroys_a_running_thread(
    harness: _Harness, qapp
) -> None:
    harness.gate = threading.Event()
    source = harness.source_skill("shutdown-skill")
    harness.open()
    harness.prompts.kind = SOURCE_FOLDER
    harness.prompts.folder = str(source)
    harness.request(IMPORT_LOCAL)
    assert _pump(qapp, lambda: bool(harness.staging_roots)), "staging never started"

    assert harness.controller._imports._runner.busy is True
    # The job cannot finish inside the wait, so shutdown must detach the
    # thread rather than let its object be destroyed underneath it.
    harness.controller._imports.shutdown(timeout_ms=100)
    assert import_worker._LINGERING_THREADS
    assert import_worker._LINGERING_THREADS[-1].isRunning()

    harness.gate.set()
    assert _pump(qapp, lambda: harness.staging_survivors() == [])
    assert harness.prompts.views == []
    assert harness.installed == []


def test_shutdown_after_a_finished_import_clears_the_thread(harness: _Harness, qapp) -> None:
    source = harness.source_skill("clean-exit")
    harness.open()
    assert harness.import_folder(source)

    assert _pump(qapp, lambda: harness.controller._imports._runner.busy is False)
    harness.controller.shutdown()
    assert harness.controller._imports._runner._thread is None
    assert import_worker._LINGERING_THREADS == []


# ── the flow stays off the GUI thread and says so ────────────────────────────


def test_the_window_stays_responsive_and_busy_while_an_import_runs(
    harness: _Harness, qapp
) -> None:
    harness.gate = threading.Event()
    _write_skill(harness.project_dir, "already-here", description="Installed already.")
    source = harness.source_skill("slow-skill")
    window = harness.open()
    harness.prompts.kind = SOURCE_FOLDER
    harness.prompts.folder = str(source)
    harness.request(IMPORT_LOCAL)

    assert _pump(qapp, lambda: bool(harness.staging_roots)), "staging never started"
    # The worker owns the blocking work; the GUI thread is still ours.
    assert harness.controller._imports._runner.busy is True
    assert window.import_actions_enabled() is False
    assert "slow-skill" in window.import_status_text()
    window.set_search_text("already")
    assert window.visible_row_ids()["project"] == ("project:already-here",)
    window.set_search_text("")

    harness.gate.set()
    assert _pump(qapp, lambda: not harness.controller._imports.is_active())
    assert window.import_actions_enabled() is True
    assert window.import_status_text() == ""


def test_only_one_import_may_be_active_at_a_time(harness: _Harness, qapp) -> None:
    harness.gate = threading.Event()
    source = harness.source_skill("first-import")
    harness.open()
    harness.prompts.kind = SOURCE_FOLDER
    harness.prompts.folder = str(source)
    harness.request(IMPORT_LOCAL)
    assert _pump(qapp, lambda: bool(harness.staging_roots))

    harness.request(IMPORT_GITHUB)  # programmatic, while the first is running

    assert harness.dialogs.warnings
    assert "importing a skill right now" in harness.dialogs.warnings[-1]
    assert "url" not in harness.prompts.asked

    harness.gate.set()
    assert _pump(qapp, lambda: not harness.controller._imports.is_active())


# ── mutation is refused whenever someone else owns the skills on disk ────────


def test_import_actions_are_disabled_and_refused_during_a_turn(harness: _Harness) -> None:
    source = harness.source_skill("turn-blocked")
    window = harness.open()

    harness.controller.set_execution_active(True)
    assert window.import_actions_enabled() is False

    harness.prompts.kind = SOURCE_FOLDER
    harness.prompts.folder = str(source)
    harness.request(IMPORT_LOCAL)  # programmatic invocation, not a click

    assert harness.prompts.asked == []
    assert harness.installed == []
    assert harness.dialogs.warnings
    message = harness.dialogs.warnings[-1]
    assert "importing, replacing, enabling, disabling, or uninstalling" in message

    harness.controller.set_execution_active(False)
    assert window.import_actions_enabled() is True


def test_clicking_an_import_button_during_a_turn_does_nothing(harness: _Harness) -> None:
    window = harness.open()
    harness.controller.set_execution_active(True)

    window._import_local_btn.click()
    window._import_github_btn.click()

    assert harness.prompts.asked == []
    assert harness.dialogs.warnings == []  # the disabled button never even asks


def test_lifecycle_mutations_are_refused_while_an_import_owns_staged_state(
    harness: _Harness,
) -> None:
    _write_skill(harness.project_dir, "victim", description="Installed already.")
    source = harness.source_skill("incoming")
    harness.open()

    seen: list[str] = []

    def _try_to_uninstall(_view) -> None:
        harness.controller._on_uninstall_requested("project:victim")
        harness.controller._on_enable_toggle_requested("project:victim", False)
        seen.append("tried")

    harness.prompts.on_review = _try_to_uninstall
    harness.prompts.decision = ImportDecision.CANCEL

    assert harness.import_folder(source)

    assert seen == ["tried"]
    assert (harness.project_dir / "victim" / "SKILL.md").is_file()
    assert harness.dialogs.warnings
    assert "importing a skill right now" in harness.dialogs.warnings[-1]
    assert harness.dialogs.questions == []  # never even asked to confirm


# ── a session belongs to the workspace it started in ─────────────────────────


def test_a_rebind_during_review_cannot_install_into_the_new_workspace(
    harness: _Harness,
) -> None:
    source = harness.source_skill("bound-skill")
    harness.open()

    def _rebind(_view) -> None:
        harness.controller.set_workspace_root(harness.other_workspace)

    harness.prompts.on_review = _rebind
    harness.prompts.decision = ImportDecision.INSTALL

    assert harness.import_folder(source)

    assert harness.prompts.closed_reviews == 1
    assert harness.installed == []
    assert not (harness.project_dir / "bound-skill").exists()
    other_project = harness.other_workspace / ".aura" / "skills" / "authored" / "bound-skill"
    assert not other_project.exists()
    assert harness.staging_survivors() == []


# ── what success does, and what it deliberately does not do ──────────────────


def test_success_refreshes_the_inventory_and_selects_the_installed_row(
    harness: _Harness,
) -> None:
    source = harness.source_skill("revealed-skill", description="Newly imported.")
    window = harness.open()

    assert harness.import_folder(source)

    assert "project:revealed-skill" in harness.install_ids()
    assert window.current_install_id() == "project:revealed-skill"
    assert "revealed-skill" in window.details_text()


def test_a_search_filter_does_not_hide_the_freshly_installed_row(harness: _Harness) -> None:
    _write_skill(harness.project_dir, "unrelated", description="Something else.")
    source = harness.source_skill("found-skill", description="Newly imported.")
    window = harness.open()
    window.set_search_text("unrelated")

    assert harness.import_folder(source)

    assert window.current_install_id() == "project:found-skill"


def test_a_successful_import_adds_no_composer_chip(harness: _Harness) -> None:
    source = harness.source_skill("not-selected")
    harness.open()

    assert harness.import_folder(source)

    assert harness.chip_ids() == ()
    row = harness.controller._rows["project:not-selected"]
    assert row.selectable is True  # the user may still choose to use it


def test_replacing_a_skill_keeps_its_unsent_composer_chip(harness: _Harness) -> None:
    _install_existing(harness, "chipped", "ORIGINAL")
    harness.open()
    harness.controller._on_use_requested("project:chipped")
    assert harness.chip_ids() == ("project:chipped",)

    source = harness.source_skill("chipped", description="The incoming one.")
    assert harness.import_folder(source, decision=ImportDecision.REPLACE)

    assert harness.installed == ["project:chipped"]
    assert harness.chip_ids() == ("project:chipped",)


# ── nothing user-facing carries an internal path ─────────────────────────────


def test_no_internal_path_reaches_the_review_or_an_error(harness: _Harness, qapp) -> None:
    source = harness.source_skill("private-paths", description="Handles widgets.")
    harness.open()
    harness.prompts.decision = ImportDecision.CANCEL

    assert harness.import_folder(source)
    view = harness.prompts.views[0]
    rendered = _rendered(view, qapp)

    for leak in (str(harness.tmp_path), str(harness.project_dir), str(harness.sources)):
        assert leak not in rendered
    assert view.source_label == "private-paths"

    # A backend refusal carrying a real path is redacted before it is shown.
    missing = harness.sources / "gone-missing"
    assert harness.import_folder(missing)
    assert harness.prompts.errors
    error = harness.prompts.errors[-1]
    assert "<path>" in error
    assert str(harness.sources) not in error
