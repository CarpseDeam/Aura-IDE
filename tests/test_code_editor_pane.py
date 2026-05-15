from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from aura.gui.code_editor_pane import CodeEditorPane
from aura.gui.smooth_code_streamer import SmoothCodeStreamer


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class CountingPlainTextEdit(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.set_plain_text_calls = 0

    def setPlainText(self, text: str) -> None:  # noqa: N802
        self.set_plain_text_calls += 1
        super().setPlainText(text)


def test_animation_region_expands_replacements_to_full_lines() -> None:
    old = "one\nold value\nthree\n"
    new = "one\nnew value\nthree\n"

    old_start, old_end, new_start, new_end = CodeEditorPane._compute_animation_region(
        old, new
    )

    assert old[old_start:old_end] == "old value\n"
    assert new[new_start:new_end] == "new value\n"


def test_animation_region_keeps_pure_insert_old_range_empty() -> None:
    old = "one\nthree\n"
    new = "one\ntwo\nthree\n"

    old_start, old_end, new_start, new_end = CodeEditorPane._compute_animation_region(
        old, new
    )

    assert old_start == old_end
    assert new[new_start:new_end] == "two\n"


def test_animation_region_keeps_inline_delete_as_char_range() -> None:
    old = "alpha beta gamma\n"
    new = "alpha gamma\n"

    old_start, old_end, new_start, new_end = CodeEditorPane._compute_animation_region(
        old, new
    )

    assert old[old_start:old_end] == "beta "
    assert new_start == new_end


def test_animation_region_handles_deleted_line_overlap() -> None:
    old = "one\ntwo\nthree\n"
    new = "one\nthree\n"

    old_start, old_end, new_start, new_end = CodeEditorPane._compute_animation_region(
        old, new
    )

    assert old[old_start:old_end] == "two\n"
    assert new_start == new_end == len("one\n")


def test_smooth_streamer_append_tick_does_not_replace_document(qapp) -> None:
    editor = CountingPlainTextEdit()
    streamer = SmoothCodeStreamer(editor)
    streamer.set_text_immediately("abc")
    replace_calls = editor.set_plain_text_calls

    streamer.set_target("abcdef")
    streamer._tick(100)

    assert editor.toPlainText() == "abcdef"
    assert editor.set_plain_text_calls == replace_calls


def test_smooth_streamer_target_updates_coalesce(qapp) -> None:
    editor = QPlainTextEdit()
    streamer = SmoothCodeStreamer(editor)

    streamer.set_target("abc")
    streamer.set_target("abcdef")
    streamer._tick(100)

    assert editor.toPlainText() == "abcdef"
    assert streamer.visible_text() == "abcdef"


def test_smooth_streamer_finalize_catches_up_to_target(qapp) -> None:
    editor = QPlainTextEdit()
    streamer = SmoothCodeStreamer(editor)
    target = "0123456789" * 20

    streamer.set_target(target)
    streamer.finish()
    while streamer.is_active():
        streamer._tick(1000)

    assert editor.toPlainText() == target
    assert streamer.visible_text() == target


def test_smooth_streamer_non_append_target_falls_back_safely(qapp) -> None:
    editor = QPlainTextEdit()
    streamer = SmoothCodeStreamer(editor)
    streamer.set_text_immediately("abc")

    streamer.set_target("xyz")
    streamer._tick(100)

    assert editor.toPlainText() == "xyz"
    assert streamer.visible_text() == "xyz"


def test_close_worker_tabs_stops_streamers(qapp) -> None:
    pane = CodeEditorPane()
    pane.open_or_focus_tab("tool-1", "demo.py")
    pane.stream_content("tool-1", "print('hello')\n")

    state = pane._typing_state["tool-1"]
    assert state["streamer"].is_active()

    pane.close_worker_tabs()

    assert pane._typing_state == {}
    assert pane._editors == {}
    assert pane._tabs.count() == 0


def test_repeated_writes_to_same_path_keep_aliases(qapp) -> None:
    pane = CodeEditorPane()
    pane.open_or_focus_tab("tool-1", "demo.py")
    pane.stream_content("tool-1", "first\n")
    pane.open_or_focus_tab("tool-2", "demo.py")
    pane.stream_content("tool-2", "second\n")

    assert pane._canonical_tool_id("tool-2") == "tool-1"
    assert pane._tabs.count() == 1

    pane.finalize_tab("tool-2")
    pane.finalize_tab("tool-1")

    state = pane._typing_state["tool-1"]
    while state["streamer"].is_active():
        state["streamer"]._tick(1000)

    assert pane._tabs.tabText(0) == "demo.py ✓"
