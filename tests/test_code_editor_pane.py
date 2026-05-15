from __future__ import annotations

from aura.gui.code_editor_pane import CodeEditorPane


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
