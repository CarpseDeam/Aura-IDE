"""Explicit installed-skill selections across send, History, Retry, and runtime."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from aura.client import ApiError, Done
from aura.context_gearbox.runtime import compose_system_prompt
from aura.conversation.history import (
    EXPLICIT_INSTALLED_SKILL_IDS_KEY,
    History,
)
from aura.conversation.manager import ConversationManager
from aura.conversation.persistence import load_conversation, save_conversation
from aura.conversation.tools.registry import ToolRegistry
from aura.gui.composer_skills import ComposerSkill
from aura.gui.input_panel import Attachment, SendPayload
from aura.gui.send_handler import SendHandler
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams
from aura.skills.library import SkillLibrary
from aura.skills.turn_state import STATUS_EXPLICIT_PREACTIVATED

_QT_APP: QApplication | None = None


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


class _Input:
    def __init__(self) -> None:
        self.queued = 0
        self.restored = None
        self.clear_skill_calls = 0

    def set_queued_messages(self, count: int) -> None:
        self.queued = count

    def restore_payload(self, payload: SendPayload) -> None:
        self.restored = payload

    def clear_selected_skills(self) -> None:
        self.clear_skill_calls += 1


class _Chat:
    def __init__(self) -> None:
        self.errors = []

    def add_error(self, *args, **kwargs) -> None:
        self.errors.append((args, kwargs))

    def add_user(self, *args, **kwargs) -> None:
        pass

    def scroll_to_bottom(self, *args, **kwargs) -> None:
        pass

    def begin_assistant(self) -> None:
        pass

    def reset(self) -> None:
        pass


class _Bridge:
    def __init__(self, workspace: Path) -> None:
        self.history = History()
        self.running = False
        self.send_skill_ids = []
        self.target_file_calls = []
        self.registry = ToolRegistry(workspace)

    def is_running(self) -> bool:
        return self.running

    def send(self, **_kwargs) -> None:
        self.send_skill_ids.append(
            self.history.latest_real_user_explicit_installed_skill_ids()
        )

    def authorize_external_reads(self, paths) -> tuple:
        return tuple(paths or ())

    def clear_external_read_authorization(self) -> None:
        self.registry.clear_external_read_authorization()

    def set_turn_target_files(self, paths) -> None:
        self.target_file_calls.append(tuple(paths))


def _handler(tmp_path: Path, monkeypatch) -> tuple[SendHandler, _Bridge, _Input, _Chat]:
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration", lambda _provider: True
    )
    bridge = _Bridge(tmp_path)
    input_panel = _Input()
    chat = _Chat()
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=input_panel,
        settings=SimpleNamespace(provider="test"),
        workspace_root=tmp_path,
    )
    handler._model_supports_vision = lambda _model: True
    return handler, bridge, input_panel, chat


def _skill(install_id: str, label: str | None = None) -> ComposerSkill:
    return ComposerSkill(install_id, label or install_id)


def _install(workspace: Path, name: str, body: str = "Follow this full procedure.") -> Path:
    directory = workspace / ".aura" / "skills" / "authored" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: focused\n---\n# Procedure\n\n{body}\n",
        encoding="utf-8",
    )
    return directory


def test_normal_send_stores_ids_on_text_and_multimodal_history(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _input, _chat = _handler(tmp_path, monkeypatch)
    chosen = (_skill("project:first"), _skill("personal:second"))

    handler.handle_send(SendPayload("text", [], chosen), "model", "off")
    handler.handle_send(
        SendPayload(
            "image",
            [Attachment("image", "shot.png", "YWJj", None)],
            chosen,
        ),
        "model",
        "off",
    )

    assert bridge.history.messages[0][EXPLICIT_INSTALLED_SKILL_IDS_KEY] == [
        "project:first",
        "personal:second",
    ]
    assert isinstance(bridge.history.messages[1]["content"], list)
    assert bridge.history.messages[1][EXPLICIT_INSTALLED_SKILL_IDS_KEY] == [
        "project:first",
        "personal:second",
    ]
    api_messages = bridge.history.for_api()
    assert EXPLICIT_INSTALLED_SKILL_IDS_KEY not in api_messages[0]
    assert EXPLICIT_INSTALLED_SKILL_IDS_KEY not in api_messages[1]


def test_history_metadata_survives_reload_and_defaults_empty_for_older_messages(
    tmp_path: Path,
) -> None:
    history = History()
    history.append_user_text(
        "selected",
        explicit_installed_skill_ids=("project:first", "personal:second"),
    )
    path = save_conversation(history, tmp_path, "model", "off")
    loaded = load_conversation(path).history

    assert loaded.latest_real_user_explicit_installed_skill_ids() == (
        "project:first",
        "personal:second",
    )
    loaded.append_user_text("legacy")
    assert loaded.latest_real_user_explicit_installed_skill_ids() == ()


def test_queued_sends_preserve_independent_frozen_selections(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, input_panel, _chat = _handler(tmp_path, monkeypatch)
    bridge.running = True
    first = SendPayload("first", [], (_skill("project:first"),))
    second = SendPayload("second", [], (_skill("project:second"),))

    handler.handle_send(first, "model-a", "off")
    handler.handle_send(second, "model-b", "high")
    bridge.running = False
    handler.process_message_queue("ignored", "off")
    handler.process_message_queue("ignored", "off")

    assert bridge.send_skill_ids == [("project:first",), ("project:second",)]
    assert input_panel.queued == 0


def test_retry_uses_retained_ids_and_next_unselected_turn_has_none(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _input, _chat = _handler(tmp_path, monkeypatch)
    handler.handle_send(
        SendPayload("selected", [], (_skill("project:original"),)),
        "model",
        "off",
    )
    bridge.history.append_assistant({"role": "assistant", "content": "failed"})

    assert handler.handle_retry_last("model", "off") is True
    handler.handle_send(SendPayload("ordinary", []), "model", "off")

    assert bridge.send_skill_ids == [
        ("project:original",),
        ("project:original",),
        (),
    ]


def test_conversation_change_clears_unsent_chips(tmp_path: Path, monkeypatch) -> None:
    handler, _bridge, input_panel, _chat = _handler(tmp_path, monkeypatch)

    handler.clear_queue()

    assert input_panel.clear_skill_calls == 1


def test_prompt_and_manager_freeze_the_same_explicit_preactivated_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    _install(workspace, "selected", "USE THE COMPLETE SELECTED BODY.")

    composed = compose_system_prompt(
        workspace, explicit_install_ids=("project:selected",)
    )
    manager = ConversationManager(History(), ToolRegistry(workspace))
    manager.configure_runtime_context(
        workspace, explicit_install_ids=("project:selected",)
    )
    state = manager._build_skill_turn_state()

    assert "### Explicitly Selected Skills" in composed.system_prompt
    assert "USE THE COMPLETE SELECTED BODY." in composed.system_prompt
    assert state is not None
    candidate = state.candidates[0]
    assert candidate.install_id == "project:selected"
    assert state.activation_log()[0]["status"] == STATUS_EXPLICIT_PREACTIVATED

    pack_entry = next(e for e in composed.ledger if e.source_id == "skill_pack")
    candidate_entry = next(e for e in composed.ledger if e.source_id == candidate.skill_id)
    assert f"explicit_chars={candidate.explicit_chars}" in candidate_entry.detail
    assert "explicitly_preactivated" in candidate_entry.detail
    assert "install_id=project:selected" in candidate_entry.detail
    assert "explicit_chars=" in pack_entry.detail
    assert candidate_entry.char_count == candidate.explicit_chars
    assert ".aura" not in candidate_entry.reason
    assert ".aura" not in candidate_entry.detail


def _run_manager(
    workspace: Path, explicit_ids: tuple[str, ...]
) -> tuple[list[dict], list[object]]:
    history = History()
    history.append_user_text("run", explicit_installed_skill_ids=explicit_ids)
    manager = ConversationManager(history, ToolRegistry(workspace))
    manager.configure_runtime_context(
        workspace, content="run", explicit_install_ids=explicit_ids
    )
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        yield Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "should not run"},
        )

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    events = []
    try:
        manager.send(
            on_event=events.append,
            approval_cb=lambda _request: None,
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
        )
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)
    return calls, events


@pytest.mark.parametrize("case", ["missing", "disabled", "conflict"])
def test_invalid_stored_explicit_references_make_zero_provider_calls(
    tmp_path: Path, monkeypatch, case: str
) -> None:
    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / case
    workspace.mkdir()
    if case == "missing":
        explicit_ids = ("project:missing",)
    elif case == "disabled":
        _install(workspace, "disabled")
        SkillLibrary(workspace).set_enabled("project:disabled", False)
        explicit_ids = ("project:disabled",)
    else:
        _install(workspace, "first", "identical body")
        _install(workspace, "second", "identical body")
        explicit_ids = ("project:first", "project:second")

    calls, events = _run_manager(workspace, explicit_ids)

    assert calls == []
    errors = [event for event in events if isinstance(event, ApiError)]
    assert len(errors) == 1
    for install_id in explicit_ids[1:] or explicit_ids:
        assert install_id in errors[0].message
    assert "Retry" in errors[0].message

    composed = compose_system_prompt(workspace, explicit_install_ids=explicit_ids)
    unresolved = [
        entry for entry in composed.ledger if entry.kind == "explicit_skill_reference"
    ]
    assert unresolved
    assert all(entry.included is False and entry.char_count == 0 for entry in unresolved)
    assert all("explicit_unresolved" in entry.detail for entry in unresolved)


def test_no_explicit_runtime_path_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    _install(workspace, "automatic")
    manager = ConversationManager(History(), ToolRegistry(workspace))
    manager.configure_runtime_context(workspace, content="unrelated request")

    state = manager._build_skill_turn_state()
    before = compose_system_prompt(workspace, content="unrelated request")
    after = compose_system_prompt(
        workspace, content="unrelated request", explicit_install_ids=()
    )

    assert before == after
    assert state is None or all(not candidate.explicit for candidate in state.candidates)


def _wait_for_bridge(bridge, qapp) -> None:
    finished = []
    bridge.finished.connect(lambda: finished.append(True))
    deadline = time.monotonic() + 5
    while not finished and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert finished, "ConversationBridge did not finish"
    qapp.processEvents()


def test_bridge_freezes_history_selection_for_prompt_and_runtime(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    from aura.bridge.qt_bridge import ConversationBridge

    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    _install(workspace, "selected", "BRIDGE-FROZEN FULL BODY.")
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(workspace)
    bridge.history.append_user_text(
        "Use it.", explicit_installed_skill_ids=("project:selected",)
    )
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        yield Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "done"},
        )

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    try:
        bridge.send(model="test-model", thinking="off")
        _wait_for_bridge(bridge, qapp)
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)
        bridge.shutdown()

    assert bridge._turn_explicit_install_ids == ("project:selected",)
    assert "BRIDGE-FROZEN FULL BODY." in calls[0]["messages"][0]["content"]
    state = bridge._manager._last_skill_turn
    assert state is not None
    assert [candidate.install_id for candidate in state.candidates if candidate.explicit] == [
        "project:selected"
    ]


def test_bridge_refuses_missing_stored_selection_before_provider_request(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    from aura.bridge.qt_bridge import ConversationBridge

    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(workspace)
    bridge.history.append_user_text(
        "Use it.", explicit_installed_skill_ids=("project:missing",)
    )
    calls = []
    errors = []
    bridge.apiError.connect(lambda _status, message: errors.append(message))

    def stream(**kwargs):
        calls.append(kwargs)
        yield Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "must not run"},
        )

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    try:
        bridge.send(model="test-model", thinking="off")
        _wait_for_bridge(bridge, qapp)
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)
        bridge.shutdown()

    assert calls == []
    assert len(errors) == 1
    assert "project:missing" in errors[0]
    assert "Retry" in errors[0]
    assert bridge.history.latest_real_user_explicit_installed_skill_ids() == (
        "project:missing",
    )
