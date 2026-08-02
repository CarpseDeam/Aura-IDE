"""Phase 1: explicit target files survive from the user request to the bridge.

The recovery plan's rule is narrow and worth restating, because most of these
tests exist to pin the *negative* half of it:

    Preserve paths the user actually named. Never guess.

So these cover both directions — a named path must arrive at the bridge intact,
and an unnamed, nonexistent, or out-of-workspace path must never be invented.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from aura.conversation.history import History
from aura.conversation.target_files import extract_target_files
from aura.gui.input_panel import Attachment, SendPayload
from aura.gui.send_handler import SendHandler


# --------------------------------------------------------------------------
# extraction unit tests
# --------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace with a few real files to resolve against."""
    for relpath in (
        "aura/conversation/pre_edit_loop_guard.py",
        "tests/test_pre_edit_loop_guard.py",
        "aura/gui/send_handler.py",
        "README.md",
    ):
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def test_named_paths_are_extracted_in_order(workspace: Path) -> None:
    text = (
        "Please fix aura/conversation/pre_edit_loop_guard.py "
        "and tests/test_pre_edit_loop_guard.py"
    )
    assert extract_target_files(text, workspace) == (
        "aura/conversation/pre_edit_loop_guard.py",
        "tests/test_pre_edit_loop_guard.py",
    )


def test_backslash_paths_are_normalized(workspace: Path) -> None:
    text = r"look at aura\gui\send_handler.py please"
    assert extract_target_files(text, workspace) == ("aura/gui/send_handler.py",)


def test_absolute_path_inside_workspace_becomes_relative(workspace: Path) -> None:
    absolute = workspace / "aura" / "gui" / "send_handler.py"
    assert extract_target_files(f"edit {absolute}", workspace) == (
        "aura/gui/send_handler.py",
    )


def test_markdown_and_punctuation_wrapping_is_stripped(workspace: Path) -> None:
    text = "Fix `aura/gui/send_handler.py`, then (README.md)."
    assert extract_target_files(text, workspace) == (
        "aura/gui/send_handler.py",
        "README.md",
    )


def test_duplicate_mentions_collapse(workspace: Path) -> None:
    text = "README.md is wrong. Fix README.md and re-read README.md."
    assert extract_target_files(text, workspace) == ("README.md",)


def test_bare_filename_at_workspace_root_resolves(workspace: Path) -> None:
    assert extract_target_files("update README.md", workspace) == ("README.md",)


# ---- the "never guess" half ----------------------------------------------


def test_no_paths_named_yields_no_targets(workspace: Path) -> None:
    """A topic-only request must not have files inferred for it."""
    text = "the pre edit loop guard keeps letting broad discovery through"
    assert extract_target_files(text, workspace) == ()


def test_nonexistent_path_is_rejected(workspace: Path) -> None:
    assert extract_target_files("fix aura/does/not/exist.py", workspace) == ()


def test_bare_filename_not_at_root_is_not_searched_for(workspace: Path) -> None:
    """send_handler.py exists under aura/gui/, but resolving a bare name to it
    would be a search — and a search can pick the wrong file."""
    assert extract_target_files("fix send_handler.py", workspace) == ()


def test_absolute_path_outside_workspace_is_rejected(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    assert extract_target_files(f"read {outside}", workspace) == ()


def test_parent_traversal_escape_is_rejected(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    assert extract_target_files("read ../escaped.py", workspace) == ()


def test_directory_is_not_a_target_file(workspace: Path) -> None:
    """Only files are targets; a directory cannot be preloaded."""
    (workspace / "aura" / "conversation.d").mkdir(parents=True, exist_ok=True)
    assert extract_target_files("look in aura/conversation.d", workspace) == ()


def test_prose_that_looks_path_shaped_is_dropped(workspace: Path) -> None:
    """Tokens only survive if they resolve to a real workspace file."""
    text = "e.g. version 3.11 broke it, see item 2.b in the notes"
    assert extract_target_files(text, workspace) == ()


def test_no_workspace_root_yields_no_targets(workspace: Path) -> None:
    assert extract_target_files("fix aura/gui/send_handler.py", None) == ()


def test_empty_text_yields_no_targets(workspace: Path) -> None:
    assert extract_target_files("", workspace) == ()
    assert extract_target_files(None, workspace) == ()


def test_extraction_is_bounded(tmp_path: Path) -> None:
    """A pasted traceback naming many real files must not blow up preload."""
    relpaths = []
    for i in range(30):
        path = tmp_path / f"mod_{i}.py"
        path.write_text("x = 1\n", encoding="utf-8")
        relpaths.append(f"mod_{i}.py")

    extracted = extract_target_files(" ".join(relpaths), tmp_path)
    assert 0 < len(extracted) <= 12
    # Bounded, but still the first ones the user wrote — not an arbitrary slice.
    assert extracted[0] == "mod_0.py"


# --------------------------------------------------------------------------
# handoff: SendHandler -> ConversationBridge
# --------------------------------------------------------------------------


class _FakeBridge:
    def __init__(self) -> None:
        self.history = History()
        self.send_calls: list[dict] = []
        self.target_file_calls: list[tuple[str, ...]] = []

    def is_running(self) -> bool:
        return False

    def set_turn_target_files(self, target_files) -> None:
        self.target_file_calls.append(tuple(target_files))

    def send(self, **kwargs) -> None:
        self.send_calls.append(kwargs)


class _FakeChat:
    def __init__(self) -> None:
        self.errors: list[tuple] = []

    def add_user(self, text: str, images=None) -> None:
        pass

    def add_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def scroll_to_bottom(self, force: bool = False) -> None:
        pass

    def begin_assistant(self) -> None:
        pass

    def reset(self) -> None:
        pass


class _FakeInput:
    def set_queued_messages(self, count: int) -> None:
        pass

    def setEnabled(self, enabled: bool) -> None:
        pass

    def set_placeholder(self, text: str) -> None:
        pass


def _make_handler(monkeypatch, workspace: Path) -> SendHandler:
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda provider: True,
    )
    handler = SendHandler(
        bridge=_FakeBridge(),
        chat=_FakeChat(),
        input_panel=_FakeInput(),
        settings=SimpleNamespace(max_tool_rounds=3, provider="test"),
        workspace_root=workspace,
    )
    handler._get_current_model_info = lambda model: None
    return handler


def test_named_target_reaches_the_bridge(monkeypatch, workspace: Path) -> None:
    """Plan test 1: a request naming a file arrives at the bridge with it."""
    handler = _make_handler(monkeypatch, workspace)
    handler.handle_send(
        SendPayload("Fix aura/conversation/pre_edit_loop_guard.py", []),
        model="m",
        thinking="off",
    )
    assert handler._bridge.target_file_calls[-1] == (
        "aura/conversation/pre_edit_loop_guard.py",
    )
    assert handler._bridge.send_calls


def test_attachment_reference_reaches_the_bridge(monkeypatch, workspace: Path) -> None:
    """Attachment refs are folded into the sent text, so their paths count."""
    handler = _make_handler(monkeypatch, workspace)
    handler.handle_send(
        SendPayload(
            "fix this",
            [
                Attachment(
                    kind="file",
                    name="send_handler.py",
                    b64=None,
                    text_ref="[user attached: aura/gui/send_handler.py]",
                )
            ],
        ),
        model="m",
        thinking="off",
    )
    assert handler._bridge.target_file_calls[-1] == ("aura/gui/send_handler.py",)


def test_unscoped_request_declares_no_targets(monkeypatch, workspace: Path) -> None:
    """Plan test 6: no guessed paths are invented for a vague request."""
    handler = _make_handler(monkeypatch, workspace)
    handler.handle_send(
        SendPayload("make the loop guard stricter", []),
        model="m",
        thinking="off",
    )
    assert handler._bridge.target_file_calls[-1] == ()


def test_outside_workspace_path_is_rejected_at_send(
    monkeypatch, workspace: Path, tmp_path: Path
) -> None:
    """Plan test 5: paths outside the workspace never become targets."""
    outside = tmp_path.parent / "outside_at_send.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    handler = _make_handler(monkeypatch, workspace)
    handler.handle_send(
        SendPayload(f"read {outside} and summarize it", []),
        model="m",
        thinking="off",
    )
    assert handler._bridge.target_file_calls[-1] == ()


def test_targets_do_not_leak_into_the_next_turn(monkeypatch, workspace: Path) -> None:
    """A scoped turn followed by an unscoped one must clear the scope."""
    handler = _make_handler(monkeypatch, workspace)
    handler.handle_send(
        SendPayload("Fix aura/gui/send_handler.py", []),
        model="m",
        thinking="off",
    )
    assert handler._bridge.target_file_calls[-1] == ("aura/gui/send_handler.py",)

    handler.handle_send(
        SendPayload("thanks, what does that module do?", []),
        model="m",
        thinking="off",
    )
    assert handler._bridge.target_file_calls[-1] == ()


def test_retry_recovers_targets_from_the_retained_turn(
    monkeypatch, workspace: Path
) -> None:
    """Retry re-derives scope from the real user turn, not from handler state."""
    handler = _make_handler(monkeypatch, workspace)
    handler.handle_send(
        SendPayload("Fix aura/conversation/pre_edit_loop_guard.py", []),
        model="m",
        thinking="off",
    )
    history = handler._bridge.history
    history.append_assistant({"role": "assistant", "content": "looking"})
    history.append_internal_user_text("reread README.md before editing")

    assert handler.handle_retry_last(model="m", thinking="off") is True

    # The internal nudge named README.md; the real request did not.
    assert handler._bridge.target_file_calls[-1] == (
        "aura/conversation/pre_edit_loop_guard.py",
    )
