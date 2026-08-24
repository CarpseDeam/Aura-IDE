"""Focused tests for literal user-path external read authorization."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from aura.client import Done, ToolCallStart
from aura.conversation.external_paths import (
    extract_absolute_path_candidates,
    extract_external_read_paths,
)
from aura.conversation.history import LITERAL_COMPOSER_TEXT_KEY, History
from aura.conversation.persistence import load_conversation, save_conversation
from aura.conversation.tools.registry import ToolRegistry
from aura.gui.input_panel import Attachment, SendPayload
from aura.gui.send_handler import SendHandler
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    external = tmp_path / "old-project"
    workspace.mkdir()
    external.mkdir()
    return workspace, external


_WINDOWS_FILESYSTEM = pytest.mark.skipif(
    os.name != "nt",
    reason="filesystem authorization tests use the Windows path syntax supported by Aura",
)


# ── A. syntax-only extraction ────────────────────────────────────────────────


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


def test_several_paths_are_extracted_in_order() -> None:
    prompt = r'Compare C:\Projects\Alpha with "C:\My Docs\notes.md" and C:\Projects\Beta.'

    assert extract_absolute_path_candidates(prompt) == [
        r"C:\Projects\Alpha",
        r"C:\My Docs\notes.md",
        r"C:\Projects\Beta",
    ]


# ── B. filesystem-backed extraction ──────────────────────────────────────────


@_WINDOWS_FILESYSTEM
def test_existing_external_windows_directory_is_detected(tmp_path: Path) -> None:
    workspace, external = _paths(tmp_path)
    assert extract_external_read_paths(f"{external}\nLook at it", workspace) == [
        external.resolve()
    ]


@_WINDOWS_FILESYSTEM
def test_existing_external_windows_slash_directory_is_detected(tmp_path: Path) -> None:
    workspace, external = _paths(tmp_path)
    slash_form = str(external).replace("\\", "/")
    assert slash_form in extract_absolute_path_candidates(slash_form)
    assert extract_external_read_paths(slash_form, workspace) == [external.resolve()]


@_WINDOWS_FILESYSTEM
def test_quoted_and_backticked_paths_with_spaces_are_detected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "My Projects" / "Old App"
    workspace.mkdir()
    external.mkdir(parents=True)

    assert extract_external_read_paths(f'"{external}".', workspace) == [external.resolve()]
    assert extract_external_read_paths(f"`{external}`", workspace) == [external.resolve()]


@_WINDOWS_FILESYSTEM
def test_an_external_file_authorizes_itself(tmp_path: Path) -> None:
    workspace, external = _paths(tmp_path)
    file_path = external / "design doc.md"
    file_path.write_text("# notes\n", encoding="utf-8")

    assert extract_external_read_paths(f'"{file_path}"', workspace) == [
        file_path.resolve()
    ]


@_WINDOWS_FILESYSTEM
def test_a_directory_and_a_file_inside_it_are_both_returned(tmp_path: Path) -> None:
    workspace, external = _paths(tmp_path)
    file_path = external / "README.md"
    file_path.write_text("x", encoding="utf-8")

    extracted = extract_external_read_paths(f'"{external}" and "{file_path}"', workspace)

    assert extracted == [external.resolve(), file_path.resolve()]


@_WINDOWS_FILESYSTEM
def test_several_distinct_external_locations_are_all_returned(tmp_path: Path) -> None:
    workspace, first = _paths(tmp_path)
    second = tmp_path / "another-project"
    second.mkdir()

    assert extract_external_read_paths(f'"{first}" and "{second}"', workspace) == [
        first.resolve(),
        second.resolve(),
    ]


@_WINDOWS_FILESYSTEM
def test_relative_and_workspace_absolute_paths_do_not_authorize(tmp_path: Path) -> None:
    workspace, external = _paths(tmp_path)
    (workspace / "src").mkdir()
    assert extract_external_read_paths("old-project", workspace) == []
    assert extract_external_read_paths(str(workspace), workspace) == []
    assert extract_external_read_paths(str(workspace / "src"), workspace) == []
    assert external.exists()


@_WINDOWS_FILESYSTEM
def test_a_path_that_does_not_exist_authorizes_nothing(tmp_path: Path) -> None:
    workspace, external = _paths(tmp_path)
    missing = external / "does-not-exist"

    assert extract_external_read_paths(str(missing), workspace) == []


@_WINDOWS_FILESYSTEM
def test_broad_user_locations_are_extracted_when_named(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    for name in ("Desktop", "Documents", "Downloads", "OneDrive"):
        (home / name).mkdir(parents=True)

    for candidate in (home, *(home / n for n in ("Desktop", "Documents", "Downloads", "OneDrive"))):
        assert extract_external_read_paths(f'"{candidate}"', workspace) == [
            candidate.resolve()
        ], candidate


# ── B2. the durable literal composer text ────────────────────────────────────


def test_literal_composer_text_is_stored_local_only_and_stripped_for_the_api() -> None:
    history = History()
    history.append_user_text(
        "Read the notes.\n\n[user attached: C:\\Elsewhere\\notes.txt]",
        literal_composer_text="Read the notes.",
    )

    stored = history.messages[-1]
    assert stored[LITERAL_COMPOSER_TEXT_KEY] == "Read the notes."
    assert history.latest_real_user_literal_composer_text() == "Read the notes."
    # The provider request carries the message, never Aura's own bookkeeping.
    sent = history.for_api()[-1]
    assert LITERAL_COMPOSER_TEXT_KEY not in sent
    assert sent["content"] == stored["content"]
    # Stripping the snapshot must not strip the canonical log.
    assert stored[LITERAL_COMPOSER_TEXT_KEY] == "Read the notes."


def test_literal_composer_text_is_stored_on_multimodal_turns() -> None:
    history = History()
    history.append_user_multimodal(
        [{"type": "text", "text": "Look at this."}],
        literal_composer_text="Look at this.",
    )

    assert history.latest_real_user_literal_composer_text() == "Look at this."
    assert LITERAL_COMPOSER_TEXT_KEY not in history.for_api()[-1]


def test_a_message_without_the_field_reports_no_literal_composer_text() -> None:
    history = History()
    history.append_user_text("legacy record")

    assert history.latest_real_user_literal_composer_text() is None


def test_literal_composer_text_survives_save_and_load(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    history = History()
    history.append_user_text(
        "typed text\n\n[user attached: notes.txt]",
        literal_composer_text="typed text",
    )

    path = save_conversation(history, workspace, "model", "off")
    reloaded = load_conversation(path).history

    assert reloaded.latest_real_user_literal_composer_text() == "typed text"


# ── C. the send layer ────────────────────────────────────────────────────────


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
        self.authorization_calls: list[tuple[Path, ...]] = []
        self.authorized_at_send: list[tuple[str, ...]] = []
        self.send_calls: list[dict] = []

    def is_running(self) -> bool:
        return self.running

    def authorize_external_reads(self, paths) -> tuple[Path, ...]:
        self.authorization_calls.append(tuple(paths))
        return self.registry.begin_external_read_turn(paths)

    def clear_external_read_authorization(self) -> None:
        self.registry.clear_external_read_authorization()

    def set_turn_target_files(self, _files) -> None:
        pass

    def send(self, **kwargs) -> None:
        self.authorized_at_send.append(self.registry.external_read_names)
        self.send_calls.append(kwargs)


def _handler(tmp_path: Path, monkeypatch) -> tuple[SendHandler, _Bridge, _Chat]:
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: True,
    )
    workspace, _external = _paths(tmp_path)
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
    external = tmp_path / "old-project"

    handler.handle_send(SendPayload(str(external), []), "model", "off")

    assert bridge.authorization_calls == [(external.resolve(),)]
    assert bridge.authorized_at_send == [("old-project",)]
    assert chat.errors == []


@_WINDOWS_FILESYSTEM
def test_several_user_paths_are_authorized_in_one_turn(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, chat = _handler(tmp_path, monkeypatch)
    first = tmp_path / "old-project"
    second = tmp_path / "another-project"
    second.mkdir()
    note = tmp_path / "loose note.md"
    note.write_text("x", encoding="utf-8")

    handler.handle_send(
        SendPayload(f'Compare "{first}" with "{second}" using "{note}".', []),
        "model",
        "off",
    )

    assert bridge.authorization_calls == [
        (first.resolve(), second.resolve(), note.resolve())
    ]
    assert set(bridge.authorized_at_send[0]) == {
        "old-project", "another-project", "loose note.md"
    }
    assert chat.errors == []


def test_no_path_clears_stale_authorization_for_new_turn(tmp_path: Path, monkeypatch) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    bridge.registry.begin_external_read_turn([external])

    handler.handle_send(SendPayload("Use only the active workspace.", []), "model", "off")

    assert bridge.authorization_calls == [()]
    assert bridge.authorized_at_send == [()]
    assert bridge.registry.external_read_available is False


@_WINDOWS_FILESYSTEM
def test_current_workspace_path_and_route_text_proceed_without_external_authorization(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, chat = _handler(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    prompt = f"Work in `{workspace}` on `master`. Implement the summary ... /Quantity ..."

    handler.handle_send(SendPayload(prompt, []), "model", "off")

    assert bridge.authorization_calls == [()]
    assert bridge.authorized_at_send == [()]
    assert len(bridge.send_calls) == 1
    assert chat.errors == []


@_WINDOWS_FILESYSTEM
def test_a_missing_path_does_not_block_the_turn(tmp_path: Path, monkeypatch) -> None:
    handler, bridge, chat = _handler(tmp_path, monkeypatch)

    handler.handle_send(
        SendPayload(f"Create {tmp_path / 'missing-project'} for me.", []), "model", "off"
    )

    assert len(bridge.send_calls) == 1
    assert bridge.authorization_calls == [()]
    assert chat.errors == []


@_WINDOWS_FILESYSTEM
def test_queued_message_authorizes_only_when_it_becomes_active(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    bridge.running = True

    handler.handle_send(SendPayload(str(external), []), "model", "off")
    assert bridge.authorization_calls == []

    bridge.running = False
    handler.process_message_queue("model", "off")
    assert bridge.authorization_calls == [(external.resolve(),)]


@_WINDOWS_FILESYSTEM
def test_a_queued_message_rederives_its_own_paths(tmp_path: Path, monkeypatch) -> None:
    """A queued turn must not inherit the authority of the turn before it."""
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"

    handler.handle_send(SendPayload(str(external), []), "model", "off")
    bridge.running = True
    handler.handle_send(SendPayload("Now just the workspace, please.", []), "model", "off")
    bridge.running = False
    handler.process_message_queue("model", "off")

    assert bridge.authorization_calls == [(external.resolve(),), ()]
    assert bridge.registry.external_read_available is False


def _reload_conversation(bridge: _Bridge, workspace: Path) -> None:
    """Round-trip the conversation through Aura's own save/open path.

    Mirrors ``ConversationPersistence.apply_loaded``: the canonical message
    dicts are written to disk and restored onto the live history, which is what
    reopening Aura or selecting a thread actually does.
    """
    path = save_conversation(bridge.history, workspace, "model", "off")
    loaded = load_conversation(path)
    bridge.history.messages = list(loaded.history.messages)


@_WINDOWS_FILESYSTEM
def test_retry_rederives_authorization_from_the_literal_composer_text(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    handler.handle_send(SendPayload(str(external), []), "model", "off")
    bridge.clear_external_read_authorization()
    bridge.authorization_calls.clear()

    assert handler.handle_retry_last("model", "off") is True
    assert bridge.authorization_calls == [(external.resolve(),)]
    assert bridge.registry.external_read_available is True


@_WINDOWS_FILESYSTEM
def test_a_typed_path_still_authorizes_retry_after_switching_conversations(
    tmp_path: Path, monkeypatch
) -> None:
    """The literal text outlives the volatile per-send state.

    ``clear_queue()`` runs whenever the active conversation changes. The
    authority for a later retry comes from the stored user message, so the
    typed path must survive it.
    """
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    handler.handle_send(SendPayload(str(external), []), "model", "off")

    handler.clear_queue()
    assert bridge.registry.external_read_available is False
    bridge.authorization_calls.clear()

    assert handler.handle_retry_last("model", "off") is True
    assert bridge.authorization_calls == [(external.resolve(),)]
    assert bridge.registry.external_read_available is True


@_WINDOWS_FILESYSTEM
def test_a_typed_path_still_authorizes_retry_after_save_and_reload(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    external = tmp_path / "old-project"
    handler.handle_send(SendPayload(str(external), []), "model", "off")

    _reload_conversation(bridge, workspace)
    handler.clear_queue()
    bridge.authorization_calls.clear()

    assert handler.handle_retry_last("model", "off") is True
    assert bridge.authorization_calls == [(external.resolve(),)]
    assert bridge.registry.external_read_available is True


def test_finalize_send_authorizes_from_submitted_text(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"

    handler._finalize_send(SendPayload(str(external), []), "model", "off")

    assert bridge.authorization_calls == [(external.resolve(),)]
    assert bridge.authorized_at_send == [("old-project",)]


def test_attachment_metadata_cannot_authorize_external_access(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"

    handler.handle_send(
        SendPayload(
            "Use the attached notes.",
            [Attachment(kind="file", name="notes.txt", b64=None, text_ref=str(external))],
        ),
        "model",
        "off",
    )

    assert bridge.authorization_calls == [()]
    assert bridge.authorized_at_send == [()]
    # The attachment block reached history, but never the allowlist.
    assert str(external) in bridge.history.latest_real_user_text()
    assert bridge.registry.external_read_available is False


def _send_attachment_only_path(handler: SendHandler, external: Path) -> None:
    """Submit a turn whose only mention of *external* is attachment metadata."""
    handler.handle_send(
        SendPayload(
            "Use the attached notes.",
            [Attachment(kind="file", name="notes.txt", b64=None, text_ref=str(external))],
        ),
        "model",
        "off",
    )


def test_attachment_metadata_cannot_authorize_on_retry(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    _send_attachment_only_path(handler, external)
    bridge.authorization_calls.clear()

    assert handler.handle_retry_last("model", "off") is True
    assert bridge.authorization_calls == [()]
    assert bridge.registry.external_read_available is False


def test_attachment_metadata_cannot_authorize_after_switching_conversations(
    tmp_path: Path, monkeypatch
) -> None:
    """The retry fallback that used to reach the stored text is gone.

    Once ``clear_queue()`` has run there is no volatile composer text left, and
    the stored message still carries the attachment reference block. Retry must
    read the literal-composer metadata — which never named the path — and not
    the flattened message content.
    """
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    _send_attachment_only_path(handler, external)

    handler.clear_queue()
    bridge.authorization_calls.clear()

    assert handler.handle_retry_last("model", "off") is True
    assert bridge.authorization_calls == [()]
    assert bridge.registry.external_read_available is False


def test_attachment_metadata_cannot_authorize_after_save_and_reload(
    tmp_path: Path, monkeypatch
) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    external = tmp_path / "old-project"
    _send_attachment_only_path(handler, external)

    _reload_conversation(bridge, workspace)
    handler.clear_queue()
    bridge.authorization_calls.clear()

    assert handler.handle_retry_last("model", "off") is True
    # The attachment block survived the round trip; the authority did not exist.
    assert str(external) in bridge.history.latest_real_user_text()
    assert bridge.authorization_calls == [()]
    assert bridge.registry.external_read_available is False


def test_a_legacy_user_message_authorizes_nothing_on_retry(
    tmp_path: Path, monkeypatch
) -> None:
    """A record written before the literal-composer field grants no authority."""
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    bridge.history.append_user_text(str(external))
    assert bridge.history.latest_real_user_literal_composer_text() is None

    assert handler.handle_retry_last("model", "off") is True
    assert bridge.authorization_calls == [()]
    assert bridge.registry.external_read_available is False


@_WINDOWS_FILESYSTEM
def test_changing_conversation_clears_authorization(tmp_path: Path, monkeypatch) -> None:
    handler, bridge, _chat = _handler(tmp_path, monkeypatch)
    external = tmp_path / "old-project"
    handler.handle_send(SendPayload(str(external), []), "model", "off")
    assert bridge.registry.external_read_available is True

    handler.clear_queue()

    assert bridge.registry.external_read_available is False


def test_bridge_clears_external_access_before_finished_signal(tmp_path: Path) -> None:
    from aura.bridge.qt_bridge import ConversationBridge

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    workspace, external = _paths(tmp_path)
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(workspace)
    assert bridge.authorize_external_reads([external]) == (external.resolve(),)

    observed: list[bool] = []
    bridge.finished.connect(
        lambda: observed.append(bridge.registry.external_read_available)
    )
    bridge._on_finished()

    assert observed == [False]


def test_bridge_workspace_switch_clears_external_access(tmp_path: Path) -> None:
    from aura.bridge.qt_bridge import ConversationBridge

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    workspace, external = _paths(tmp_path)
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(workspace)
    bridge.authorize_external_reads([external])

    other = tmp_path / "other-workspace"
    other.mkdir()
    bridge.set_workspace_root(other)

    assert bridge.registry.external_read_available is False


# ── D. the real send → bridge → manager path ─────────────────────────────────


class _ScriptedProvider:
    """Replays one scripted round, recording the tool catalog it was sent."""

    def __init__(self, rounds: list[list[dict] | None]) -> None:
        self._rounds = rounds
        self.catalogs: list[list[dict]] = []

    def __call__(self, **kwargs):
        index = len(self.catalogs)
        self.catalogs.append(list(kwargs.get("tools") or []))
        round_calls = self._rounds[min(index, len(self._rounds) - 1)]
        if round_calls is None:
            yield Done(
                finish_reason="stop",
                full_message={"role": "assistant", "content": "done"},
            )
            return
        for position, call in enumerate(round_calls):
            yield ToolCallStart(index=position, id=call["id"], name=call["function"]["name"])
        yield Done(
            finish_reason="tool_calls",
            full_message={"role": "assistant", "content": "", "tool_calls": round_calls},
        )


@_WINDOWS_FILESYSTEM
def test_real_send_path_freezes_the_catalog_and_reads_an_authorized_file(
    tmp_path: Path, monkeypatch
) -> None:
    """SendHandler → ConversationBridge → ConversationManager, end to end."""
    from aura.bridge.qt_bridge import ConversationBridge

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: True,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "in_workspace.py").write_text("workspace_marker = 1\n", encoding="utf-8")
    folder = tmp_path / "My Reference Notes"
    folder.mkdir()
    named = folder / "design doc.md"
    named.write_text("external_marker in the named file\n", encoding="utf-8")
    sibling = folder / "private notes.md"
    sibling.write_text("sibling only\n", encoding="utf-8")

    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(workspace)
    chat = _Chat()
    handler = SendHandler(
        bridge=bridge,
        chat=chat,
        input_panel=_Input(),
        settings=SimpleNamespace(provider="test"),
        workspace_root=workspace,
    )
    handler._get_current_model_info = lambda _model: None

    provider = _ScriptedProvider([
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": str(named)}),
                },
            },
            {
                "id": "call-2",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": str(sibling)}),
                },
            },
        ],
        None,
    ])

    done = threading.Event()
    bridge.finished.connect(done.set)

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, provider)
    try:
        handler.handle_send(
            SendPayload(f'Read "{named}" and summarize it.', []), "test-model", "off"
        )
        deadline = time.monotonic() + 30.0
        while not done.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)

    assert done.is_set(), "the production turn never finished"

    # The catalog was frozen once and is the canonical production surface.
    assert len(provider.catalogs) == 2
    assert provider.catalogs[0] == provider.catalogs[1]
    names = [tool["function"]["name"] for tool in provider.catalogs[0]]
    # The five production built-ins lead the catalog; whichever optional
    # capabilities this environment enables follow them.
    assert names[:5] == [
        "read_file", "grep_search", "update_task_checklist", "apply_patch", "shell"
    ]
    assert "read_reference_file" not in names

    # The turn's authoritative record of what each tool call returned.
    by_id = {
        message["tool_call_id"]: str(message.get("content") or "")
        for message in bridge.history.messages
        if message.get("role") == "tool"
    }
    authorized_result = by_id["call-1"]
    assert "external_marker in the named file" in authorized_result
    assert '"external": true' in authorized_result.lower()
    assert '"read_only": true' in authorized_result.lower()

    sibling_result = by_id["call-2"]
    assert "not authorized" in sibling_result
    assert "sibling only" not in sibling_result

    # Authorization is a turn capability and ends with the turn.
    assert bridge.registry.external_read_available is False
