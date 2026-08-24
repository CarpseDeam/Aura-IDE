"""The Terminal window renders one continuous, read-only transcript.

Every assertion here is about the single QPlainTextEdit document: commands,
streamed output and exit statuses accumulate chronologically, with no widget
created per tool/process id.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from aura.gui.terminal_window import TerminalWindow


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp: QApplication):
    win = TerminalWindow()
    try:
        yield win
    finally:
        win.deleteLater()
        qapp.processEvents()


def test_multiple_commands_accumulate_in_one_transcript(window: TerminalWindow) -> None:
    window.set_command("tool-1", "echo one", "C:/work")
    window.append_output("tool-1", "one output\n")
    window.set_result("tool-1", 0)
    window.set_command("tool-2", "echo two", "C:/work")
    window.append_output("tool-2", "two output\n")
    window.set_result("tool-2", 1)

    text = window.transcript_text()
    assert text == (
        "C:/work\n"
        "\u276f echo one\n"
        "one output\n"
        "\u2713 exited 0\n"
        "\n"
        "\u276f echo two\n"
        "two output\n"
        "\u2717 exited 1\n"
    )


def test_transcript_preserves_event_order_across_interleaved_ids(
    window: TerminalWindow,
) -> None:
    """A second stream's output lands after it, never reordered before it."""
    window.set_command("tool-1", "first")
    window.append_output("tool-1", "a\n")
    window.set_command("tool-2", "second")
    window.append_output("tool-2", "b\n")
    window.append_output("tool-1", "late-from-first\n")

    text = window.transcript_text()
    assert text.index("a\n") < text.index("\u276f second")
    assert text.index("\u276f second") < text.index("b\n")
    assert text.index("b\n") < text.index("late-from-first")


def test_cwd_rendered_initially_and_only_when_it_changes(
    window: TerminalWindow,
) -> None:
    window.set_command("tool-1", "one", "C:/work")
    window.set_result("tool-1", 0)
    window.set_command("tool-2", "two", "C:/work")
    window.set_result("tool-2", 0)
    window.set_command("tool-3", "three", "C:/work/sub")

    text = window.transcript_text()
    assert text.count("C:/work\n") == 1
    assert text.count("C:/work/sub\n") == 1
    assert text.index("C:/work/sub\n") > text.index("\u276f two")


def test_output_accumulates_while_hidden(window: TerminalWindow) -> None:
    assert window.isVisible() is False
    window.set_command("tool-1", "long job", "C:/work")
    window.append_output("tool-1", "streamed while hidden\n")
    window.set_result("tool-1", 0)

    assert "streamed while hidden" in window.transcript_text()
    assert window.isVisible() is False


def test_clear_empties_transcript_and_emits_signal(window: TerminalWindow) -> None:
    cleared: list[bool] = []
    window.terminal_cleared.connect(lambda: cleared.append(True))

    window.set_command("tool-1", "echo one", "C:/work")
    window.append_output("tool-1", "some output\n")
    assert window.transcript_text() != ""

    window.clear()

    assert window.transcript_text() == ""
    assert cleared == [True]
    # A reused id renders again after a reset, and so does the cwd.
    window.set_command("tool-1", "echo one", "C:/work")
    assert "C:/work" in window.transcript_text()


def test_clear_button_clears_the_transcript(window: TerminalWindow) -> None:
    window.set_command("tool-1", "echo one", "C:/work")
    window.append_output("tool-1", "some output\n")

    window._clear_btn.click()

    assert window.transcript_text() == ""


def test_started_and_finished_signals_carry_exit_codes(
    window: TerminalWindow,
) -> None:
    started: list[bool] = []
    finished: list[int] = []
    window.terminal_started.connect(lambda: started.append(True))
    window.terminal_finished.connect(finished.append)

    window.set_command("tool-1", "ok", "C:/work")
    window.set_result("tool-1", 0)
    window.set_command("tool-2", "bad", "C:/work")
    window.set_result("tool-2", 3)

    assert started == [True, True]
    assert finished == [0, 3]


def test_output_for_unknown_id_is_ignored(window: TerminalWindow) -> None:
    window.append_output("never-started", "orphan output\n")
    window.set_result("never-started", 0)

    assert window.transcript_text() == ""


def test_document_growth_is_globally_bounded(window: TerminalWindow) -> None:
    window.set_command("tool-1", "noisy", "C:/work")
    chunk = "".join(f"line {i}\n" for i in range(1000))
    for _ in range(20):
        window.append_output("tool-1", chunk)
    window.transcript_text()

    assert window._view.document().blockCount() <= TerminalWindow.MAX_BLOCKS


def test_auto_follows_when_already_at_the_bottom(window: TerminalWindow) -> None:
    window._view.resize(400, 120)
    window.set_command("tool-1", "noisy", "C:/work")
    window.append_output("tool-1", "".join(f"line {i}\n" for i in range(200)))
    window.transcript_text()

    scrollbar = window._view.verticalScrollBar()
    assert scrollbar.maximum() > 0
    assert scrollbar.value() == scrollbar.maximum()


def test_scrolling_up_is_not_undone_by_new_output(window: TerminalWindow) -> None:
    window._view.resize(400, 120)
    window.set_command("tool-1", "noisy", "C:/work")
    window.append_output("tool-1", "".join(f"line {i}\n" for i in range(200)))
    window.transcript_text()

    scrollbar = window._view.verticalScrollBar()
    scrollbar.setValue(0)
    assert window._auto_follow is False

    window.append_output("tool-1", "".join(f"more {i}\n" for i in range(200)))
    window.transcript_text()

    assert scrollbar.value() == 0

    # Returning to the bottom re-arms auto-follow.
    scrollbar.setValue(scrollbar.maximum())
    assert window._auto_follow is True
    window.append_output("tool-1", "tail\n")
    window.transcript_text()
    assert scrollbar.value() == scrollbar.maximum()


def test_geometry_is_saved_and_restored(qapp: QApplication) -> None:
    source = TerminalWindow()
    saved: list[str] = []
    source.geometry_saved.connect(saved.append)
    source.resize(720, 380)
    source._save_geometry()
    source.deleteLater()

    assert saved and saved[-1]
    restored = TerminalWindow(initial_geometry=saved[-1])
    try:
        assert restored.size().width() == 720
        assert restored.size().height() == 380
    finally:
        restored.deleteLater()
        qapp.processEvents()


def test_visibility_signal_and_hide_on_close(window: TerminalWindow, qapp) -> None:
    seen: list[bool] = []
    window.visibility_changed.connect(seen.append)

    window.show_and_raise()
    qapp.processEvents()
    assert window.is_open() is True

    window.close()
    qapp.processEvents()
    assert window.is_open() is False
    assert seen == [True, False]


def test_no_per_command_widgets_or_nested_editors(window: TerminalWindow) -> None:
    window.set_command("tool-1", "echo one", "C:/work")
    window.set_command("tool-2", "echo two", "C:/work")
    window.transcript_text()

    editors = window.findChildren(QPlainTextEdit)
    assert editors == [window._view]
    assert not hasattr(window, "_terminal_cards")
    assert not hasattr(window, "_card_layout")


class _RecordingTerminal:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str, str]] = []

    def set_command(self, tool_id: str, command: str, cwd: str = "") -> None:
        self.commands.append((tool_id, command, cwd))


def test_playground_threads_starting_cwd_into_the_transcript() -> None:
    """The routed starting cwd must reach the window instead of being dropped."""
    from aura.gui.playground import AuraPlayground

    playground = SimpleNamespace(_terminal_window=_RecordingTerminal())
    AuraPlayground.start_terminal_command(playground, "call-1", "echo hi", "C:/work")

    assert playground._terminal_window.commands == [("call-1", "echo hi", "C:/work")]
