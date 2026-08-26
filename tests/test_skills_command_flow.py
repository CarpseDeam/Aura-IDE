"""The two ways into the Skills manager: the composer button and /skills.

``/skills`` is one of Aura's own literal commands: it runs entirely locally,
works before any provider is configured, and leaves the conversation exactly
as it found it — no chat bubble, no History entry, no provider request. It
also must not cost the user the skills they had already picked for their
next message, which is true of every built-in command: the composer clears
its payload on submit, but a local command never spends a model turn.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from aura.gui.builtin_commands import classify_built_in_command
from aura.gui.composer_skills import ComposerSkill
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.main_window_signal_wiring import MainWindowSignalWiring
from aura.gui.send_handler import SendHandler
from aura.gui.skills_manager import SkillsManagerController
from aura.skills.library import SkillLibrary


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Chat:
    def __init__(self) -> None:
        self.users: list[str] = []
        self.infos: list[tuple] = []
        self.errors: list[tuple] = []

    def add_user(self, *args, **kwargs) -> None:
        self.users.append(args[0] if args else "")

    def add_info(self, *args, **kwargs) -> None:
        self.infos.append(args)

    def add_error(self, *args, **kwargs) -> None:
        self.errors.append(args)

    def scroll_to_bottom(self, *args, **kwargs) -> None:
        pass

    def begin_assistant(self) -> None:
        pass


class _History:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def append_user_text(self, *args, **kwargs) -> None:
        self.messages.append({"role": "user"})

    def append_user_multimodal(self, *args, **kwargs) -> None:
        self.messages.append({"role": "user"})


class _Bridge:
    def __init__(self) -> None:
        self.history = _History()
        self.sends: list[dict] = []
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def send(self, **kwargs) -> None:
        self.sends.append(kwargs)

    def authorize_external_reads(self, paths) -> tuple:
        return tuple(paths or ())

    def set_turn_target_files(self, paths) -> None:
        pass


def _wired(
    tmp_path: Path,
    monkeypatch,
    *,
    provider_configured: bool = True,
) -> SimpleNamespace:
    """Compose the composer, send handler, and manager as MainWindow does."""
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: provider_configured,
    )
    workspace = tmp_path / "workspace"
    (workspace / ".aura" / "skills" / "authored").mkdir(parents=True, exist_ok=True)
    panel = InputPanel(workspace)
    chat = _Chat()
    bridge = _Bridge()
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=panel,
        settings=SimpleNamespace(provider="test"),
        workspace_root=workspace,
    )
    controller = SkillsManagerController(
        input_panel=panel,
        workspace_root=workspace,
        library_factory=lambda root: SkillLibrary(
            root,
            personal_dir=tmp_path / "personal",
            bundled_dir=tmp_path / "bundled",
        ),
    )
    opens: list[int] = []
    original_open = controller.open_manager

    def _counted_open() -> None:
        opens.append(1)
        original_open()

    controller.open_manager = _counted_open  # type: ignore[method-assign]

    # Exactly the two connections MainWindowSignalWiring makes.
    panel.sent.connect(lambda payload: handler.handle_send(payload, "model", "off"))
    panel.skills_requested.connect(controller.open_manager)
    handler.skills_manager_requested.connect(controller.open_manager)

    drones: list[int] = []
    handler.drone_bay_requested.connect(lambda: drones.append(1))

    return SimpleNamespace(
        workspace=workspace,
        panel=panel,
        chat=chat,
        bridge=bridge,
        handler=handler,
        controller=controller,
        opens=opens,
        drones=drones,
    )


def _write_skill(workspace: Path, name: str) -> Path:
    folder = workspace / ".aura" / "skills" / "authored" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A focused procedure.\n---\n# Do it\n\nCarefully.\n",
        encoding="utf-8",
    )
    return folder


# ── literal recognition ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["/skills", "  /skills  ", "/SKILLS"])
def test_literal_skills_command_is_recognised(text: str) -> None:
    assert classify_built_in_command(text) == "skills"


@pytest.mark.parametrize(
    "text",
    [
        "/skills list",
        "/skill",
        "skills",
        "what skills do you have?",
        "add a /skills button to the composer",
        "please explain how /skills works and then refactor the manager",
        "/skills\nand also fix the parser",
    ],
)
def test_near_misses_stay_ordinary_prompts(text: str) -> None:
    assert classify_built_in_command(text) is None


def test_existing_built_in_commands_are_unchanged() -> None:
    assert classify_built_in_command("/undo") == "undo"
    assert classify_built_in_command("/drone") == "drone_enter_mode"
    assert classify_built_in_command("git status") == "git_status"
    assert classify_built_in_command("fix the failing widget test") is None


# ── local execution ──────────────────────────────────────────────────────────


def test_skills_opens_the_manager_without_a_configured_provider(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    wired = _wired(tmp_path, monkeypatch, provider_configured=False)
    _write_skill(wired.workspace, "local-only")

    wired.handler.handle_send(SendPayload("/skills", []), "model", "off")

    assert wired.opens == [1]
    assert wired.controller.window is not None
    assert wired.controller.window.isVisible()
    assert wired.chat.errors == []


def test_skills_touches_neither_history_provider_nor_chat(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    wired = _wired(tmp_path, monkeypatch)

    wired.handler.handle_send(SendPayload("/skills", []), "model", "off")

    assert wired.bridge.history.messages == []
    assert wired.bridge.sends == []
    assert wired.chat.users == []
    assert wired.chat.infos == []
    assert wired.chat.errors == []


def test_skills_without_a_workspace_stays_a_local_error(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    wired = _wired(tmp_path, monkeypatch)
    wired.handler.set_workspace_root(None)

    wired.handler.handle_send(SendPayload("/skills", []), "model", "off")

    assert wired.opens == []
    assert wired.bridge.sends == []
    assert [args[0] for args in wired.chat.errors] == ["No workspace"]


def test_an_ordinary_message_mentioning_skills_still_sends(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    wired = _wired(tmp_path, monkeypatch)

    wired.handler.handle_send(
        SendPayload("document how /skills works", []), "model", "off"
    )

    assert wired.opens == []
    assert len(wired.bridge.sends) == 1
    assert len(wired.bridge.history.messages) == 1


# ── the composer keeps what a local command never spent ──────────────────────


@pytest.mark.parametrize("command", ["/skills", "/drone"])
def test_selected_chips_survive_a_local_command(
    tmp_path: Path, monkeypatch, qapp, command: str
) -> None:
    wired = _wired(tmp_path, monkeypatch)
    wired.panel.select_installed_skill("project:first", "first")
    wired.panel.select_installed_skill("personal:second", "second")
    wired.panel.set_text(command)

    wired.panel._on_submit()

    assert [skill.install_id for skill in wired.panel.selected_skills()] == [
        "project:first",
        "personal:second",
    ]
    assert [skill.label for skill in wired.panel.selected_skills()] == ["first", "second"]
    # The command itself did run, so its text is not restored.
    assert wired.panel._editor.toPlainText() == ""


def test_a_real_send_still_consumes_its_chips(tmp_path: Path, monkeypatch, qapp) -> None:
    wired = _wired(tmp_path, monkeypatch)
    wired.panel.select_installed_skill("project:first", "first")
    wired.panel.set_text("fix the widget test")

    wired.panel._on_submit()

    assert wired.panel.selected_skills() == ()
    assert len(wired.bridge.history.messages) == 1


def test_queued_local_command_restores_its_own_frozen_selection(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    wired = _wired(tmp_path, monkeypatch)
    payload = SendPayload("/skills", [], (ComposerSkill("project:frozen", "frozen"),))

    wired.handler.handle_send(payload, "model", "off")

    assert [skill.install_id for skill in wired.panel.selected_skills()] == [
        "project:frozen"
    ]


# ── one controller behind both entry points ──────────────────────────────────


def test_button_and_command_reach_the_same_controller(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    wired = _wired(tmp_path, monkeypatch)
    _write_skill(wired.workspace, "shared-entry")

    QTest.mouseClick(wired.panel._skills_btn, Qt.MouseButton.LeftButton)
    from_button = wired.controller.window
    wired.handler.handle_send(SendPayload("/skills", []), "model", "off")

    assert wired.opens == [1, 1]
    assert from_button is not None
    assert wired.controller.window is from_button
    assert from_button.visible_row_ids()["project"] == ("project:shared-entry",)


def test_skills_button_is_visible_and_needs_a_workspace(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    wired = _wired(tmp_path, monkeypatch)

    assert wired.panel._skills_btn.text() == "Skills"
    assert "next message" in wired.panel._skills_btn.toolTip()
    assert wired.panel._skills_btn.isEnabled() is True

    wired.panel.set_workspace_root(None)
    assert wired.panel._skills_btn.isEnabled() is False

    wired.panel.set_workspace_root(wired.workspace)
    assert wired.panel._skills_btn.isEnabled() is True


def test_main_window_wires_both_entry_points_to_the_skills_controller() -> None:
    source = inspect.getsource(MainWindowSignalWiring.wire)

    assert "w._input.skills_requested.connect(w._skills_controller.open_manager)" in source
    assert (
        "w._send_handler.skills_manager_requested.connect(w._skills_controller.open_manager)"
        in source
    )
