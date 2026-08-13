"""Focused tests for literal user-path reference authorization."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from aura.conversation.history import History
from aura.conversation.reference_paths import (
    ReferencePathError,
    extract_absolute_path_candidates,
    extract_reference_path,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.gui.input_panel import Attachment, SendPayload
from aura.gui.send_handler import SendHandler


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    reference = tmp_path / "old-project"
    workspace.mkdir()
    reference.mkdir()
    return workspace, reference


_WINDOWS_FILESYSTEM = pytest.mark.skipif(
    os.name != "nt",
    reason="filesystem authorization tests use the Windows path syntax supported by Aura",
)


def test_windows_drive_backslash_form_is_recognized_as_absolute_syntax() -> None:
    assert extract_absolute_path_candidates(r"C:\Projects\Foo") == [
        r"C:\Projects\Foo"
    ]


def test_windows_drive_slash_form_is_recognized_as_absolute_syntax() -> None:
    assert extract_absolute_path_candidates("C:/Projects/Foo") == ["C:/Projects/Foo"]


def test_quoted_and_backticked_windows_paths_with_spaces_are_detected() -> None:
    quoted = r"C:\Projects With Spaces\Old App"
    backticked = "C:/Projects With Spaces/Old App"

    assert extract_absolute_path_candidates(f'"{quoted}"') == [quoted]
    assert extract_absolute_path_candidates(f"`{backticked}`") == [backticked]


def test_windows_unc_path_is_recognized_as_absolute_syntax() -> None:
    assert extract_absolute_path_candidates(r"\\server\share\Project") == [
        r"\\server\share\Project"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "/Quantity",
        "/api/orders",
        "/users",
        "/foo",
        "Price/Quantity",
        "foo/bar",
        "src/module.py",
        "https://example.com/api/orders",
        "//server/share/Project",
    ],
)
def test_non_windows_path_shaped_prompt_text_is_not_authority(text: str) -> None:
    assert extract_absolute_path_candidates(text) == []


def test_market_summary_prompt_does_not_extract_route_text_as_a_path() -> None:
    prompt = r"""# Implement deterministic target MarketStateSummary

Work in `C:\Projects\Lantern` on `master`.

Starting HEAD must be:

`acdfd19cd26efcb9bcadfc573ca2bab1ae13e654`

Implement Lantern's first compact deterministic market-state summary.

... /Quantity ...
"""

    assert extract_absolute_path_candidates(prompt) == [r"C:\Projects\Lantern"]


@_WINDOWS_FILESYSTEM
def test_existing_external_windows_directory_is_detected(tmp_path: Path) -> None:
    workspace, reference = _paths(tmp_path)
    assert extract_reference_path(f"{reference}\nLook at it", workspace) == reference.resolve()


@_WINDOWS_FILESYSTEM
def test_existing_external_windows_slash_directory_is_detected(tmp_path: Path) -> None:
    workspace, reference = _paths(tmp_path)
    slash_form = str(reference).replace("\\", "/")
    assert slash_form in extract_absolute_path_candidates(slash_form)
    assert extract_reference_path(slash_form, workspace) == reference.resolve()


@_WINDOWS_FILESYSTEM
def test_quoted_and_backticked_paths_with_spaces_are_detected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    reference = tmp_path / "My Projects" / "Old App"
    workspace.mkdir()
    reference.mkdir(parents=True)

    assert extract_reference_path(f'"{reference}".', workspace) == reference.resolve()
    assert extract_reference_path(f"`{reference}`", workspace) == reference.resolve()


@_WINDOWS_FILESYSTEM
def test_relative_and_workspace_absolute_paths_do_not_authorize(tmp_path: Path) -> None:
    workspace, reference = _paths(tmp_path)
    assert extract_reference_path("old-project", workspace) is None
    assert extract_reference_path(str(workspace), workspace) is None
    assert extract_reference_path(str(workspace / "src"), workspace) is None
    assert reference.exists()


@_WINDOWS_FILESYSTEM
def test_missing_external_path_fails_locally(tmp_path: Path) -> None:
    workspace, reference = _paths(tmp_path)
    missing = reference / "does-not-exist"
    try:
        extract_reference_path(str(missing), workspace)
    except ReferencePathError as exc:
        assert "existing directory" in str(exc)
    else:
        raise AssertionError("expected missing external path to fail")


@_WINDOWS_FILESYSTEM
def test_external_file_path_does_not_authorize(tmp_path: Path) -> None:
    workspace, reference = _paths(tmp_path)
    file_path = reference / "project.godot"
    file_path.write_text("[application]\n", encoding="utf-8")
    try:
        extract_reference_path(str(file_path), workspace)
    except ReferencePathError as exc:
        assert "existing directory" in str(exc)
    else:
        raise AssertionError("expected external file path to fail")


@_WINDOWS_FILESYSTEM
def test_multiple_distinct_external_directories_fail(tmp_path: Path) -> None:
    workspace, first = _paths(tmp_path)
    second = tmp_path / "another-project"
    second.mkdir()
    try:
        extract_reference_path(f'"{first}" and "{second}"', workspace)
    except ReferencePathError as exc:
        assert "one external reference project" in str(exc)
    else:
        raise AssertionError("expected multiple reference directories to fail")


class _Chat:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.users: list[tuple[str, object]] = []
        self.assistant_started = 0

    def add_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def add_user(self, text: str, images=None) -> None:
        self.users.append((text, images))

    def scroll_to_bottom(self, force: bool = False) -> None:
        pass

    def begin_assistant(self) -> None:
        self.assistant_started += 1

    def reset(self) -> None:
        pass


class _Input:
    def set_queued_messages(self, _count: int) -> None:
        pass

    def setEnabled(self, _enabled: bool) -> None:
        pass

    def set_placeholder(self, _text: str) -> None:
        pass


class _Bridge:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.history = History()
        self.running = False
        self.authorization_calls: list[Path | None] = []
        self.authorization_available_at_send: list[bool] = []
        self.send_calls: list[dict] = []

    def is_running(self) -> bool:
        return self.running

    def authorize_reference_root(self, candidate: Path | None) -> tuple[bool, str]:
        self.authorization_calls.append(candidate)
        return self.registry.begin_reference_turn(candidate)

    def clear_reference_authorization(self) -> None:
        self.registry.clear_reference_authorization()

    def set_turn_target_files(self, _files) -> None:
        pass

    def send(self, **kwargs) -> None:
        self.authorization_available_at_send.append(
            self.registry.reference_root_available
        )
        self.send_calls.append(kwargs)


def _handler(tmp_path: Path, monkeypatch) -> tuple[SendHandler, _Bridge, _Chat]:
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: True,
    )
    workspace, _reference = _paths(tmp_path)
    bridge = _Bridge(ToolRegistry(workspace))
    chat = _Chat()
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=_Input(),
        settings=SimpleNamespace(provider="test"),
        workspace_root=workspace,
    )
    handler._get_current_model_info = lambda _model: None
    return handler, bridge, chat


@_WINDOWS_FILESYSTEM
def test_valid_user_path_is_authorized_before_send(tmp_path: Path, monkeypatch) -> None:
    handler, bridge, chat = _handler(tmp_path, monkeypatch)
    reference = tmp_path / "old-project"

    handler.handle_send(SendPayload(str(reference), []), "model", "off")

    assert bridge.authorization_calls == [reference.resolve()]
    assert bridge.authorization_available_at_send == [True]
    assert chat.errors == []


def test_no_path_clears_stale_authorization_for_new_turn(tmp_path: Path, monkeypatch) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    reference = tmp_path / "old-project"
    bridge.registry.begin_reference_turn(reference)

    handler.handle_send(SendPayload("Use only the active workspace.", []), "model", "off")

    assert bridge.authorization_calls == [None]
    assert bridge.authorization_available_at_send == [False]


@_WINDOWS_FILESYSTEM
def test_current_workspace_path_and_route_text_proceed_without_external_authorization(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, chat = _handler(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    prompt = f"Work in `{workspace}` on `master`. Implement the summary ... /Quantity ..."

    handler.handle_send(SendPayload(prompt, []), "model", "off")

    assert bridge.authorization_calls == [None]
    assert bridge.authorization_available_at_send == [False]
    assert len(bridge.send_calls) == 1
    assert chat.errors == []


@_WINDOWS_FILESYSTEM
def test_invalid_user_path_stops_before_history_or_model_send(tmp_path: Path, monkeypatch) -> None:
    handler, bridge, chat = _handler(tmp_path, monkeypatch)

    handler.handle_send(
        SendPayload(str(tmp_path / "missing-project"), []), "model", "off"
    )

    assert bridge.send_calls == []
    assert bridge.history.messages == []
    assert chat.assistant_started == 0
    assert chat.errors and "existing directory" in chat.errors[0][1]


@_WINDOWS_FILESYSTEM
def test_queued_message_authorizes_only_when_it_becomes_active(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    reference = tmp_path / "old-project"
    bridge.running = True

    handler.handle_send(SendPayload(str(reference), []), "model", "off")
    assert bridge.authorization_calls == []

    bridge.running = False
    handler.process_message_queue("model", "off")
    assert bridge.authorization_calls == [reference.resolve()]


@_WINDOWS_FILESYSTEM
def test_retry_rederives_authorization_from_retained_user_text(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    reference = tmp_path / "old-project"
    handler.handle_send(SendPayload(str(reference), []), "model", "off")
    bridge.clear_reference_authorization()
    bridge.authorization_calls.clear()

    assert handler.handle_retry_last("model", "off") is True
    assert bridge.authorization_calls == [reference.resolve()]


def test_vision_description_cannot_authorize_a_reference(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    reference = tmp_path / "old-project"

    handler._finalize_send(
        SendPayload("Explain this screenshot", []),
        "model",
        "off",
        [f"The screenshot mentions {reference}"],
        None,
    )

    assert bridge.authorization_calls == [None]
    assert bridge.authorization_available_at_send == [False]


def test_attachment_metadata_cannot_authorize_a_reference(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    reference = tmp_path / "old-project"

    handler.handle_send(
        SendPayload(
            "Use the attached notes.",
            [Attachment(kind="file", name="notes.txt", b64=None, text_ref=str(reference))],
        ),
        "model",
        "off",
    )

    assert bridge.authorization_calls == [None]
    assert bridge.authorization_available_at_send == [False]


def test_bridge_clears_reference_before_finished_signal(tmp_path: Path) -> None:
    from aura.bridge.qt_bridge import ConversationBridge

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    workspace, reference = _paths(tmp_path)
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(workspace)
    ok, message = bridge.authorize_reference_root(reference)
    assert ok is True, message
    bridge.registry._reference_codebase_index = object()

    observed: list[tuple[bool, object | None]] = []
    bridge.finished.connect(
        lambda: observed.append(
            (
                bridge.registry.reference_root_available,
                bridge.registry._reference_codebase_index,
            )
        )
    )
    bridge._on_finished()

    assert observed == [(False, None)]
