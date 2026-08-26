"""Plain-language skill creation through one visible production turn."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from aura.config import AppSettings  # noqa: E402
from aura.conversation.history import History  # noqa: E402
from aura.gui.input_panel import InputPanel, SendPayload  # noqa: E402
from aura.gui.main_window import MainWindow  # noqa: E402
from aura.gui.send_handler import SendHandler  # noqa: E402
from aura.gui.skills_manager import (
    SkillsManagerController,  # noqa: E402
    import_worker,  # noqa: E402
)
from aura.gui.skills_manager.creation_controller import DraftLease  # noqa: E402
from aura.gui.skills_manager.creation_dialogs import (  # noqa: E402
    SkillCreationIntakeDialog,
    SkillCreationRequest,
)
from aura.gui.skills_manager.import_dialogs import ImportReviewDialog  # noqa: E402
from aura.gui.skills_manager.import_models import (  # noqa: E402
    SOURCE_FOLDER,
    ImportDecision,
    ImportSource,
)
from aura.skills.identity import InstallScope  # noqa: E402
from aura.skills.importer import SkillImporter  # noqa: E402
from aura.skills.library import SkillLibrary  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_lingering_import_threads(qapp):
    yield
    for thread in list(import_worker._LINGERING_THREADS):
        try:
            if thread.isRunning():
                thread.quit()
                assert thread.wait(10_000)
        except RuntimeError:
            pass
    import_worker._LINGERING_THREADS.clear()
    qapp.processEvents()


def _pump(qapp, predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.005)
    return predicate()


def _write_skill(
    folder: Path,
    name: str,
    *,
    description: str = "Explains careful widget work.",
    body: str = "# Widget procedure\n\nInspect first, then make the smallest useful change.\n",
    valid: bool = True,
) -> Path:
    skill = folder / name
    skill.mkdir(parents=True, exist_ok=True)
    if valid:
        content = f"---\nname: {name}\ndescription: {description}\ntriggers:\n  - widget work\n---\n{body}"
    else:
        content = "---\nname: Not Valid\n---\n"
    (skill / "SKILL.md").write_text(content, encoding="utf-8")
    return skill


class _CreationPrompts:
    def __init__(self) -> None:
        self.request = SkillCreationRequest("Help with widgets")
        self.errors: list[str] = []
        self.ask_count = 0

    def ask(self, _parent):
        self.ask_count += 1
        return self.request

    def show_error(self, _parent, message: str) -> None:
        self.errors.append(message)


class _ImportPrompts:
    def __init__(self) -> None:
        self.decision = ImportDecision.INSTALL
        self.views: list = []
        self.errors: list[str] = []
        self.closed_reviews = 0

    def review(self, _parent, view):
        self.views.append(view)
        return self.decision

    def close_review(self) -> None:
        self.closed_reviews += 1

    def show_error(self, _parent, title: str, message: str) -> None:
        self.errors.append(f"{title}: {message}")


class _Dialogs:
    StandardButton = QMessageBox.StandardButton

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, _parent, title, text, *_args, **_kwargs) -> None:
        self.warnings.append(f"{title}: {text}")


class _RecordingImporter(SkillImporter):
    def __init__(self, library, harness) -> None:
        super().__init__(library)
        self._harness = harness

    def preview_from_folder(self, folder, *, destination_scope):
        self._harness.preview_calls.append((Path(folder), destination_scope))
        if self._harness.preview_gate is not None:
            self._harness.preview_entered.set()
            assert self._harness.preview_gate.wait(timeout=10)
        return super().preview_from_folder(folder, destination_scope=destination_scope)

    def install(self, preview, *, replace=False):
        self._harness.install_calls.append((preview.destination_scope, replace))
        if self._harness.fail_install:
            raise RuntimeError("deterministic install failure")
        return super().install(preview, replace=replace)


class _Harness:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.workspace = tmp_path / "workspace"
        self.other_workspace = tmp_path / "other"
        self.personal = tmp_path / "personal"
        self.bundled = tmp_path / "bundled"
        for path in (self.workspace, self.other_workspace, self.personal, self.bundled):
            path.mkdir(parents=True, exist_ok=True)
        self.creation_prompts = _CreationPrompts()
        self.import_prompts = _ImportPrompts()
        self.dialogs = _Dialogs()
        monkeypatch.setattr("aura.gui.skills_manager.controller.QMessageBox", self.dialogs)
        self.input = InputPanel(self.workspace)
        self.turns: list[tuple[str, str]] = []
        self.history_entries: list[str] = []
        self.next_turn_id = "creation-turn-1"
        self.refuse_turn = False
        self.preview_calls: list[tuple[Path, InstallScope]] = []
        self.install_calls: list[tuple[InstallScope, bool]] = []
        self.fail_install = False
        self.preview_gate: threading.Event | None = None
        self.preview_entered = threading.Event()
        self.controller = SkillsManagerController(
            input_panel=self.input,
            workspace_root=self.workspace,
            library_factory=self.library,
            importer_factory=lambda library: _RecordingImporter(library, self),
            import_prompts=self.import_prompts,
            creation_prompts=self.creation_prompts,
            start_creation_turn=self.launch,
        )
        self.controller.open_manager()

    def library(self, workspace: Path) -> SkillLibrary:
        return SkillLibrary(workspace, personal_dir=self.personal, bundled_dir=self.bundled)

    def launch(self, prompt: str, turn_id: str) -> bool:
        if self.refuse_turn:
            return False
        self.next_turn_id = turn_id
        self.turns.append((turn_id, prompt))
        self.history_entries.append(prompt)
        return True

    def start(self) -> Path:
        self.controller.window.create_requested.emit()
        assert self.controller._creation.is_active()
        prompt = self.turns[-1][1]
        match = re.search(r"`(\.aura/skills/drafts/[0-9a-f]{32})/`", prompt)
        assert match is not None
        return self.workspace / Path(match.group(1))

    def finish(self, *, successful: bool = True, turn_id: str | None = None) -> None:
        self.controller.creation_turn_finished(
            turn_id or self.next_turn_id, successful=successful
        )

    def settle(self, qapp) -> None:
        assert _pump(qapp, lambda: not self.controller._creation.is_active())

    def chips(self) -> tuple[str, ...]:
        return tuple(skill.install_id for skill in self.input.selected_skills())


@pytest.fixture()
def harness(tmp_path, monkeypatch, qapp) -> _Harness:
    manager = _Harness(tmp_path, monkeypatch)
    yield manager
    manager.controller.shutdown()
    assert _pump(qapp, lambda: not manager.controller._imports._runner._running)


def test_create_button_and_plain_language_intake(qapp) -> None:
    dialog = SkillCreationIntakeDialog()
    assert dialog.windowTitle() == "Create with Aura"
    assert dialog.offered_scopes() == (InstallScope.PROJECT, InstallScope.PERSONAL)
    assert InstallScope.BUNDLED not in dialog.offered_scopes()
    assert dialog.request().scope is InstallScope.PROJECT
    assert not dialog._create_button.isEnabled()

    dialog._description.setPlainText("Learn the project's release checklist")
    dialog._preferred_name.setText("release helper")
    assert dialog._create_button.isEnabled()
    assert dialog.request().preferred_name == "release helper"

    dialog._destination.setCurrentIndex(1)
    assert dialog.request().scope is InstallScope.PERSONAL


def test_creation_allocates_a_unique_workspace_draft_and_precise_prompt(harness) -> None:
    harness.creation_prompts.request = SkillCreationRequest(
        "Understand the local widget conventions", preferred_name="widget-helper"
    )
    first = harness.start()
    prompt = harness.turns[-1][1]
    assert first.is_dir()
    assert first.is_relative_to(harness.workspace)
    assert not first.is_relative_to(harness.workspace / ".aura" / "skills" / "authored")
    assert "apply_patch" in prompt
    assert "Inspect the current project" in prompt
    assert "exactly one valid skill" in prompt
    assert "Do not install, commit, push, execute, or test scripts" in prompt
    assert "widget-helper" in prompt
    harness.finish(successful=False)
    assert not first.exists()

    harness.next_turn_id = "creation-turn-2"
    second = harness.start()
    assert second != first
    harness.finish(successful=False)


@pytest.mark.parametrize(
    ("scope", "installed_id"),
    [
        (InstallScope.PROJECT, "project:widget-helper"),
        (InstallScope.PERSONAL, "personal:widget-helper"),
    ],
)
def test_generated_skills_use_real_importer_review_and_install(
    harness, qapp, scope, installed_id
) -> None:
    harness.creation_prompts.request = SkillCreationRequest(
        "Help with widgets", scope=scope
    )
    draft = harness.start()
    body = "# Complete instructions\n\nRead every widget before changing it.\n"
    candidate = _write_skill(draft, "widget-helper", body=body)

    harness.finish()
    harness.settle(qapp)

    assert harness.preview_calls == [(candidate, scope)]
    assert harness.install_calls == [(scope, False)]
    assert installed_id in harness.controller._rows
    assert harness.controller.window.current_install_id() == installed_id
    assert body in harness.import_prompts.views[-1].skill_markdown
    review = ImportReviewDialog(harness.import_prompts.views[-1])
    assert body in review.rendered_text()
    assert "triggers: widget work" in harness.import_prompts.views[-1].metadata_text
    assert harness.chips() == ()
    assert not draft.exists()
    if scope is InstallScope.PERSONAL:
        assert not any(path.is_relative_to(harness.personal) for path, _scope in harness.preview_calls)


def test_invalid_generation_is_reviewable_but_cannot_install(harness, qapp) -> None:
    draft = harness.start()
    _write_skill(draft, "bad-skill", valid=False)
    harness.finish()
    harness.settle(qapp)

    view = harness.import_prompts.views[-1]
    assert not view.installable
    assert harness.install_calls == []
    assert not draft.exists()


def test_conflict_requires_explicit_replace(harness, qapp) -> None:
    installed = harness.workspace / ".aura" / "skills" / "authored"
    _write_skill(installed, "widget-helper", body="# Old\n")
    harness.import_prompts.decision = ImportDecision.CANCEL
    draft = harness.start()
    _write_skill(draft, "widget-helper", body="# New\n")
    harness.finish()
    harness.settle(qapp)
    assert harness.import_prompts.views[-1].conflict
    assert harness.install_calls == []
    assert "# Old" in (installed / "widget-helper" / "SKILL.md").read_text(encoding="utf-8")

    harness.next_turn_id = "creation-turn-2"
    harness.import_prompts.decision = ImportDecision.REPLACE
    draft = harness.start()
    _write_skill(draft, "widget-helper", body="# Explicit replacement\n")
    harness.finish()
    harness.settle(qapp)
    assert harness.install_calls == [(InstallScope.PROJECT, True)]
    assert "# Explicit replacement" in (
        installed / "widget-helper" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_start_refusal_leaves_no_history_draft_or_busy_state(harness) -> None:
    harness.refuse_turn = True
    drafts = harness.workspace / ".aura" / "skills" / "drafts"
    harness.controller.window.create_requested.emit()
    assert not harness.controller._creation.is_active()
    assert not any(drafts.iterdir()) if drafts.exists() else True
    assert harness.creation_prompts.errors
    assert harness.controller.window.create_action_enabled()


def test_cancelled_intake_and_missing_workspace_never_allocate_a_draft(
    harness,
) -> None:
    harness.creation_prompts.request = None
    harness.controller._on_create_requested()
    drafts = harness.workspace / ".aura" / "skills" / "drafts"
    assert not harness.controller.creation_active()
    assert not any(drafts.iterdir()) if drafts.exists() else True

    harness.controller.set_workspace_root(None)
    harness.creation_prompts.request = SkillCreationRequest("Help with widgets")
    harness.controller._on_create_requested()
    assert not harness.controller.creation_active()
    assert harness.creation_prompts.errors


def test_programmatic_creation_is_refused_during_a_production_turn(harness) -> None:
    harness.controller.set_execution_active(True)
    harness.controller._on_create_requested()
    assert not harness.controller.creation_active()
    assert harness.turns == []
    assert harness.dialogs.warnings


def test_abandoned_import_worker_blocks_creation_until_its_slot_is_released(
    harness, qapp
) -> None:
    harness.preview_gate = threading.Event()
    source_root = harness.workspace / "slow-import-source"
    source = _write_skill(source_root, "slow-import")
    importer = harness.controller._new_importer()
    assert importer is not None
    import_source = ImportSource(
        kind=SOURCE_FOLDER,
        location=str(source),
        label="slow-import",
        scope=InstallScope.PROJECT,
    )
    assert harness.controller._imports._start_source(importer, import_source)
    assert harness.preview_entered.wait(timeout=5)

    harness.controller._imports.abandon()
    assert not harness.controller._imports.is_active()
    assert harness.controller._imports.has_outstanding_job()
    assert not harness.controller.window.create_action_enabled()
    assert not harness.controller.window.import_actions_enabled()

    asks_before = harness.creation_prompts.ask_count
    harness.controller._on_create_requested()
    drafts = harness.workspace / ".aura" / "skills" / "drafts"
    assert harness.creation_prompts.ask_count == asks_before
    assert harness.turns == []
    assert harness.history_entries == []
    assert not drafts.exists()
    assert not harness.controller.creation_active()
    assert harness.dialogs.warnings

    harness.preview_gate.set()
    assert _pump(qapp, harness.controller.window.create_action_enabled)
    assert not harness.controller._imports.has_outstanding_job()
    assert harness.controller.window.import_actions_enabled()


def test_generated_runner_refusal_releases_draft_before_completion(
    harness, monkeypatch
) -> None:
    draft = harness.start()
    _write_skill(draft, "widget-helper")
    lease = harness.controller._creation._lease
    assert lease is not None
    release = lease.release
    events: list[tuple[str, bool]] = []
    queue_holds: list[bool] = []
    refreshes: list[None] = []
    reveals: list[str] = []

    def release_and_record() -> None:
        events.append(("release", draft.exists()))
        release()

    monkeypatch.setattr(lease, "release", release_and_record)
    monkeypatch.setattr(
        harness.controller._imports._runner,
        "start",
        lambda _token, _job: False,
    )
    monkeypatch.setattr(
        harness.controller,
        "refresh",
        lambda: refreshes.append(None),
    )
    monkeypatch.setattr(
        harness.controller.window,
        "select_row",
        lambda install_id: reveals.append(install_id) or False,
    )
    harness.controller.creation_session_changed.connect(queue_holds.append)
    harness.controller._imports.generated_import_finished.connect(
        lambda _owner_id: events.append(("complete", draft.exists()))
    )

    harness.finish()

    assert events == [("release", True), ("complete", False)]
    assert not draft.exists()
    assert not harness.controller.creation_active()
    assert queue_holds[-1] is False
    assert harness.import_prompts.views == []
    assert harness.install_calls == []
    assert refreshes == []
    assert reveals == []


def test_successful_generated_worker_owns_draft_until_copy_finishes(
    harness, qapp, monkeypatch
) -> None:
    harness.preview_gate = threading.Event()
    draft = harness.start()
    _write_skill(draft, "widget-helper")
    lease = harness.controller._creation._lease
    assert lease is not None
    release = lease.release
    releases: list[bool] = []

    def release_and_record() -> None:
        releases.append(draft.exists())
        release()

    monkeypatch.setattr(lease, "release", release_and_record)
    harness.finish()
    assert harness.preview_entered.wait(timeout=5)
    assert draft.exists()
    assert releases == []

    harness.preview_gate.set()
    harness.settle(qapp)
    assert releases == [True]
    assert not draft.exists()
    assert harness.import_prompts.views
    assert harness.install_calls == [(InstallScope.PROJECT, False)]


@pytest.mark.parametrize("failure", ["stopped", "model", "missing", "review", "install"])
def test_all_failure_and_cancel_paths_clean_the_creation_draft(
    harness, qapp, failure
) -> None:
    draft = harness.start()
    if failure not in ("missing", "stopped", "model"):
        _write_skill(draft, "widget-helper")
    if failure == "review":
        harness.import_prompts.decision = ImportDecision.CANCEL
    if failure == "install":
        harness.fail_install = True

    harness.finish(successful=failure not in ("stopped", "model"))
    harness.settle(qapp)
    assert not draft.exists()
    assert not harness.controller._creation.is_active()
    if failure == "install":
        assert harness.import_prompts.errors


def test_later_turn_and_workspace_rebind_are_stale(harness, qapp) -> None:
    draft = harness.start()
    creation_turn_id = harness.turns[-1][0]
    _write_skill(draft, "widget-helper")
    harness.finish(turn_id="some-later-turn")
    assert harness.controller._creation.is_active()
    assert harness.preview_calls == []

    harness.controller.set_workspace_root(harness.other_workspace)
    harness.finish(turn_id=creation_turn_id)
    qapp.processEvents()
    assert not draft.exists()
    assert harness.preview_calls == []
    assert harness.controller._rows == {}
    assert not (harness.other_workspace / ".aura" / "skills" / "authored").exists()


def test_workspace_rebind_while_generated_preview_is_blocked_keeps_worker_lease(
    harness, qapp
) -> None:
    harness.preview_gate = threading.Event()
    draft = harness.start()
    _write_skill(draft, "widget-helper")
    harness.finish()
    assert harness.preview_entered.wait(timeout=5)

    harness.controller.set_workspace_root(harness.other_workspace)
    assert draft.exists(), "rebind deleted a draft while the importer was reading it"
    harness.preview_gate.set()

    assert _pump(qapp, lambda: not draft.exists())
    assert harness.import_prompts.views == []
    assert harness.install_calls == []
    assert harness.controller._rows == {}


def test_shutdown_while_generated_preview_is_blocked_detaches_with_cleanup_owner(
    harness, qapp
) -> None:
    harness.preview_gate = threading.Event()
    draft = harness.start()
    _write_skill(draft, "widget-helper")
    harness.finish()
    assert harness.preview_entered.wait(timeout=5)

    harness.controller._creation.shutdown()
    harness.controller._imports.shutdown(timeout_ms=50)
    assert draft.exists(), "shutdown deleted a draft while the importer was reading it"
    assert import_worker._LINGERING_THREADS

    harness.preview_gate.set()
    assert _pump(qapp, lambda: not draft.exists())
    assert harness.import_prompts.views == []
    assert harness.install_calls == []


def test_shutdown_abandons_generation_without_stale_handoff(harness, qapp) -> None:
    draft = harness.start()
    _write_skill(draft, "widget-helper")
    harness.controller.shutdown()
    harness.finish()
    qapp.processEvents()
    assert not draft.exists()
    assert harness.preview_calls == []


def test_creation_and_import_competitors_are_programmatically_refused(harness) -> None:
    harness.start()
    assert not harness.controller._creation.start()
    harness.controller._on_import_requested("local")
    assert not harness.controller._imports.is_active()
    assert not harness.controller.window.create_action_enabled()
    assert not harness.controller.window.import_actions_enabled()
    harness.finish(successful=False)


def test_draft_cleanup_refuses_a_link_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    drafts = workspace / ".aura" / "skills" / "drafts"
    drafts.mkdir(parents=True)
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    session_id = "a" * 32
    link = drafts / session_id
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    DraftLease(workspace.resolve(), session_id).release()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert os.path.lexists(link)


class _Chat:
    def __init__(self) -> None:
        self.users: list[str] = []

    def add_user(self, text, _images=None) -> None:
        self.users.append(text)

    def scroll_to_bottom(self, **_kwargs) -> None:
        pass

    def begin_assistant(self) -> None:
        pass

    def add_error(self, *_args, **_kwargs) -> None:
        pass


class _Bridge:
    def __init__(self) -> None:
        self.history = History()
        self.sent: list[tuple[str, object]] = []
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def authorize_external_reads(self, _paths) -> None:
        pass

    def set_turn_target_files(self, _paths) -> None:
        pass

    def send(self, *, model, thinking) -> None:
        self.sent.append((model, thinking))
        self.running = True


def test_main_window_creation_seam_uses_normal_visible_send_path(
    tmp_path, monkeypatch, qapp
) -> None:
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration", lambda _provider: True
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = _Bridge()
    chat = _Chat()
    panel = InputPanel(workspace)
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=panel,
        settings=AppSettings(),
        workspace_root=workspace,
    )

    class _Window:
        _workspace_root = workspace
        _skill_creation_turn_id = ""
        _skill_creation_turn_failed = False
        _bridge = bridge
        _send_handler = handler

        @staticmethod
        def current_model():
            return "selected-production-model"

        @staticmethod
        def current_thinking():
            return "high"

    prompt = "Create exactly one skill in .aura/skills/drafts/session."
    turn_id = "a" * 32
    seam = _Window()
    started = MainWindow._start_skill_creation_turn(seam, prompt, turn_id)

    assert started
    assert seam._skill_creation_turn_id == turn_id
    assert bridge.sent == [("selected-production-model", "high")]
    assert bridge.history.latest_real_user_text() == prompt
    assert chat.users == [prompt]
    assert handler._message_queue == []


def test_queue_is_held_in_fifo_order_until_creation_session_ends(
    tmp_path, monkeypatch, qapp
) -> None:
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration", lambda _provider: True
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = _Bridge()
    chat = _Chat()
    panel = InputPanel(workspace)
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=panel,
        settings=AppSettings(),
        workspace_root=workspace,
    )
    handler.set_queue_paused(True)
    handler.handle_send(SendPayload("first queued", []), "model-a", "off")
    handler.handle_send(SendPayload("second queued", []), "model-b", "high")
    handler.process_message_queue("ignored", "off")
    assert [item.text for item in handler._message_queue] == [
        "first queued",
        "second queued",
    ]

    handler.set_queue_paused(False)
    handler.process_message_queue("ignored", "off")
    assert bridge.history.latest_real_user_text() == "first queued"
    assert [item.text for item in handler._message_queue] == ["second queued"]


def test_offscreen_main_window_fake_completion_installs_and_reveals_project_skill(
    tmp_path, monkeypatch, qapp
) -> None:
    """Smoke the real MainWindow seam, visible History, and real importer."""
    workspace = tmp_path / "workspace"
    profile = tmp_path / "profile"
    workspace.mkdir()
    profile.mkdir()
    monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
    monkeypatch.setenv("AURA_DATA_DIR", str(profile))
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration", lambda _provider: True
    )
    monkeypatch.setattr("aura.gui.main_window.load_workspace_root", lambda: workspace)
    settings = AppSettings()
    settings.first_launch_done = True
    settings.restore_last_conversation = False
    monkeypatch.setattr("aura.gui.main_window.load_settings", lambda: settings)
    monkeypatch.setattr("aura.gui.main_window.save_settings", lambda _settings: None)
    monkeypatch.setattr(
        "aura.gui.main_window_update.MainWindowUpdateController.check_for_updates",
        lambda _self: None,
    )
    monkeypatch.setattr(
        "aura.gui.main_window_pricing.MainWindowPricingController.schedule_startup_refresh",
        lambda _self, delay_ms=0: False,
    )

    window = MainWindow()
    running = {"value": False}
    reviewed = _ImportPrompts()
    intake = _CreationPrompts()
    window._skills_controller._imports._prompts = reviewed
    window._skills_controller._creation._prompts = intake
    window._skills_controller.open_manager()

    def fake_send(*, model, thinking) -> None:
        assert model == window.current_model()
        assert thinking == window.current_thinking()
        running["value"] = True
        window._bridge.started.emit()
        prompt = window._bridge.history.latest_real_user_text()
        match = re.search(r"`(\.aura/skills/drafts/[0-9a-f]{32})/`", prompt)
        assert match is not None
        draft = workspace / Path(match.group(1))
        _write_skill(draft, "mainwindow-created")
        final = {"role": "assistant", "content": "The draft is ready for review."}
        window._bridge.history.append_assistant(final)

        def complete() -> None:
            window._bridge.streamDone.emit("stop", final)
            running["value"] = False
            window._bridge.finished.emit()

        QTimer.singleShot(0, complete)

    window._bridge.is_running = lambda: running["value"]
    window._bridge.send = fake_send
    try:
        window._skills_controller.window.create_requested.emit()
        assert _pump(
            qapp,
            lambda: "project:mainwindow-created" in window._skills_controller._rows,
        )
        assert not window._skills_controller.creation_active()
        assert window._skills_controller.window.current_install_id() == (
            "project:mainwindow-created"
        )
        assert window._input.selected_skills() == ()
        assert reviewed.views
        assert "# Widget procedure" in reviewed.views[-1].skill_markdown
        assert any(
            message.get("role") == "user"
            and "Create one Aura skill" in str(message.get("content"))
            for message in window._bridge.history.messages
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
