from __future__ import annotations

import pytest

from aura.focused_actions import (
    AmbiguousSelectionError,
    build_explain_selection_prompt,
    build_fix_selection_prompt,
    extract_selection_context,
)


def test_extract_selection_context_with_offsets() -> None:
    text = "\n".join(f"line {i}" for i in range(1, 61))
    selected = "line 20\nline 21"
    start = text.index(selected)
    end = start + len(selected)

    ctx = extract_selection_context("src/app.py", text, selected, start, end)

    assert ctx.start_line == 20
    assert ctx.end_line == 21
    assert ctx.language == "python"
    assert "  20: line 20" in ctx.context_text
    assert "  21: line 21" in ctx.context_text


def test_extract_selection_context_ambiguous_without_offsets() -> None:
    with pytest.raises(AmbiguousSelectionError):
        extract_selection_context("src/app.py", "x = 1\nx = 1\n", "x = 1")


def test_explain_prompt_is_read_only() -> None:
    text = "def f():\n    return 1\n"
    prompt = build_explain_selection_prompt(
        "src/app.py",
        text,
        "return 1",
        text.index("return 1"),
        text.index("return 1") + len("return 1"),
    )

    assert "Requested action:\nExplain selected code" in prompt
    assert "This is a read-only request: do not modify files." in prompt
    assert "Selected lines:\n2-2" in prompt


def test_fix_prompt_uses_normal_edit_flow() -> None:
    text = "def f():\n    return 1\n"
    prompt = build_fix_selection_prompt(
        "src/app.py",
        text,
        "return 1",
        text.index("return 1"),
        text.index("return 1") + len("return 1"),
    )

    assert "Requested action:\nFix selected code" in prompt
    assert "normal diff approval" in prompt
    assert "use patch_file" in prompt


def test_read_only_fix_prompt_becomes_suggestion_only() -> None:
    text = "def f():\n    return 1\n"
    prompt = build_fix_selection_prompt(
        "src/app.py",
        text,
        "return 1",
        text.index("return 1"),
        text.index("return 1") + len("return 1"),
        read_only_mode=True,
    )

    assert "This is a read-only request: do not modify files." in prompt
    assert "Provide explanation or suggested changes only." in prompt
