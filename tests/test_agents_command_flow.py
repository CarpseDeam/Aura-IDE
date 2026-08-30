"""``/agents`` behaves exactly like ``/skills``.

Both are Aura's own literal commands: they run entirely locally, work before
any provider is configured, and leave the conversation exactly as they found
it — no chat bubble, no History entry, no provider request. ``/agents``
reaches the same page the rail's Agents button opens, and toggles it the
same way.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.agents.local_state import AgentLocalState  # noqa: E402
from aura.agents.store import AgentStore  # noqa: E402
from aura.gui.builtin_commands import classify_built_in_command  # noqa: E402
from aura.gui.input_panel import InputPanel, SendPayload  # noqa: E402
from aura.gui.main_window_agents import MainWindowAgentsController  # noqa: E402
from aura.gui.main_window_signal_wiring import MainWindowSignalWiring  # noqa: E402
from aura.gui.send_handler import SendHandler  # noqa: E402


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


class _Tab:
    def __init__(self) -> None:
        self.checked = False

    def setChecked(self, value: bool) -> None:  # noqa: N802 - Qt naming
        self.checked = bool(value)

    def isChecked(self) -> bool:  # noqa: N802 - Qt naming
        return self.checked


@pytest.fixture()
def wired(tmp_path: Path, qapp, monkeypatch) -> SimpleNamespace:
    """The composer, send handler, and Agents controller, wired as MainWindow does."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: False,
    )
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
    tab = _Tab()
    window = SimpleNamespace(
        _workspace_root=workspace, _edge_rail=SimpleNamespace(agents_tab=tab)
    )
    controller = MainWindowAgentsController(
        window,
        workspace_root=workspace,
        store_factory=lambda root: AgentStore(root, personal_dir=tmp_path / "personal"),
        state_factory=lambda root: AgentLocalState(root, state_root=tmp_path / "userdata"),
    )

    # Exactly the two connections MainWindowSignalWiring makes for Agents.
    panel.sent.connect(lambda payload: handler.handle_send(payload, "model", "off"))
    handler.agents_requested.connect(controller.on_agents_requested)

    yield SimpleNamespace(
        workspace=workspace,
        panel=panel,
        chat=chat,
        bridge=bridge,
        handler=handler,
        controller=controller,
        tab=tab,
    )
    page = controller.agents_page
    if page is not None:
        page.close()
        page.deleteLater()


# ── literal recognition ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["/agents", "  /agents  ", "/AGENTS"])
def test_the_literal_command_is_recognised(text: str) -> None:
    assert classify_built_in_command(text) == "agents_enter_mode"


@pytest.mark.parametrize(
    "text",
    [
        "/agents list",
        "/agent",
        "agents",
        "what agents do you have?",
        "add an /agents button to the composer",
        "/agents\nand also fix the parser",
    ],
)
def test_near_misses_stay_ordinary_prompts(text: str) -> None:
    assert classify_built_in_command(text) is None


# ── local execution ──────────────────────────────────────────────────────────


def test_agents_opens_the_page_without_a_configured_provider(wired) -> None:
    wired.handler.handle_send(SendPayload("/agents", []), "model", "off")

    assert wired.controller.is_open() is True
    assert wired.chat.errors == []


def test_agents_touches_neither_history_provider_nor_chat(wired) -> None:
    wired.handler.handle_send(SendPayload("/agents", []), "model", "off")

    assert wired.bridge.history.messages == []
    assert wired.bridge.sends == []
    assert wired.chat.users == []
    assert wired.chat.infos == []
    assert wired.chat.errors == []


def test_agents_toggles_the_same_page_the_rail_opens(wired) -> None:
    wired.controller.on_agents_requested()
    from_rail = wired.controller.agents_page
    assert wired.tab.isChecked() is True

    wired.handler.handle_send(SendPayload("/agents", []), "model", "off")

    assert wired.controller.agents_page is from_rail
    assert wired.controller.is_open() is False
    assert wired.tab.isChecked() is False

    wired.handler.handle_send(SendPayload("/agents", []), "model", "off")

    assert wired.controller.agents_page is from_rail
    assert wired.controller.is_open() is True
    assert wired.tab.isChecked() is True


def test_agents_without_a_workspace_stays_a_local_error(wired) -> None:
    wired.handler.set_workspace_root(None)

    wired.handler.handle_send(SendPayload("/agents", []), "model", "off")

    assert wired.controller.agents_page is None
    assert wired.bridge.sends == []
    assert [args[0] for args in wired.chat.errors] == ["No workspace"]


def test_an_ordinary_message_mentioning_agents_still_needs_a_provider(wired) -> None:
    """Nothing about the command changed how a real request is handled."""
    wired.handler.handle_send(SendPayload("document how /agents works", []), "model", "off")

    assert wired.controller.agents_page is None
    assert wired.bridge.sends == []
    assert [args[0] for args in wired.chat.errors] == ["No AI provider configured"]


def test_the_composer_keeps_the_chips_the_command_never_spent(wired) -> None:
    wired.panel.select_installed_skill("project:first", "first")
    wired.panel.set_text("/agents")

    wired.panel._on_submit()

    assert [skill.install_id for skill in wired.panel.selected_skills()] == ["project:first"]
    assert wired.panel._editor.toPlainText() == ""
    assert wired.controller.is_open() is True


# ── one controller behind both entry points ──────────────────────────────────


def test_main_window_wires_both_entry_points_to_the_agents_controller() -> None:
    source = inspect.getsource(MainWindowSignalWiring.wire)

    assert (
        "w._send_handler.agents_requested.connect(w._agents_controller.on_agents_requested)"
        in source
    )
    assert (
        "w._edge_rail.agentsRequested.connect(w._agents_controller.on_agents_requested)"
        in source
    )


def test_the_command_never_reaches_the_built_in_action_dispatch() -> None:
    """It is handled before the provider guard, like /skills — not after it."""
    source = inspect.getsource(SendHandler._handle_built_in_action)

    assert "agents_enter_mode" not in source
