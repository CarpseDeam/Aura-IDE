"""Tests for aura.conversation.tools.fs_write — the 3-tier edit matching algorithm."""

from __future__ import annotations

from pathlib import Path
from aura.conversation.tools.fs_write import propose_write, propose_edit, _replace_line_range


# ---------------------------------------------------------------------------
# _replace_line_range
# ---------------------------------------------------------------------------

def test_replace_line_range_single_line():
    original = "line0\nline1\nline2\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = _replace_line_range(original, lines_with_nl, 1, 2, "REPLACED\n")
    assert result == "line0\nREPLACED\nline2\n"


def test_replace_line_range_multi_line():
    original = "a\nb\nc\nd\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = _replace_line_range(original, lines_with_nl, 1, 3, "X\nY\n")
    assert result == "a\nX\nY\nd\n"


def test_replace_line_range_start_of_file():
    original = "one\ntwo\nthree\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = _replace_line_range(original, lines_with_nl, 0, 1, "FIRST\n")
    assert result == "FIRST\ntwo\nthree\n"


def test_replace_line_range_end_of_file():
    original = "one\ntwo\nthree\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = _replace_line_range(original, lines_with_nl, 2, 3, "LAST\n")
    assert result == "one\ntwo\nLAST\n"


def test_replace_line_range_no_trailing_newline():
    """Handle files that don't end with a newline."""
    original = "line0\nline1\nline2"  # no trailing newline on last line
    lines_with_nl = original.splitlines(keepends=True)
    result = _replace_line_range(original, lines_with_nl, 1, 2, "CHANGED\n")
    assert result == "line0\nCHANGED\nline2"


# ---------------------------------------------------------------------------
# propose_write
# ---------------------------------------------------------------------------

def test_propose_write_new_file(tmp_workspace: Path):
    target = tmp_workspace / "new_file.py"
    result = propose_write(tmp_workspace, target, "print('hello')")
    assert result["ok"] is True
    assert result["is_new_file"] is True
    assert result["old_content"] == ""
    assert result["new_content"] == "print('hello')"
    assert result["rel_path"] == "new_file.py"


def test_propose_write_existing_file(sample_py_file: Path, tmp_workspace: Path):
    result = propose_write(tmp_workspace, sample_py_file, "replaced content")
    assert result["ok"] is True
    assert result["is_new_file"] is False
    assert "def hello()" in result["old_content"]
    assert result["new_content"] == "replaced content"
    assert result["rel_path"] == "sample.py"


def test_propose_write_binary_file(tmp_workspace: Path):
    target = tmp_workspace / "data.bin"
    target.write_bytes(b"\x00\x01\x02\x80\xff")
    result = propose_write(tmp_workspace, target, "new")
    assert result["ok"] is False
    assert "not valid UTF-8" in result["error"]


# ---------------------------------------------------------------------------
# propose_edit — Tier 1: Exact string match
# ---------------------------------------------------------------------------

def test_edit_exact_match_unique(sample_py_file: Path, tmp_workspace: Path):
    """Single occurrence — should replace via exact match."""
    result = propose_edit(tmp_workspace, sample_py_file, "hello world", "HELLO WORLD")
    assert result["ok"] is True
    assert result["match_tier"] == "exact"
    assert "HELLO WORLD" in result["new_content"]
    assert "hello world" not in result["new_content"]


def test_edit_exact_match_multiple_occurrences_falls_through(tmp_workspace: Path):
    """When old_str appears multiple times, exact match should fall through to
    line-exact or fuzzy matching."""
    f = tmp_workspace / "dup.py"
    f.write_text("DUPLICATE\nmiddle\nDUPLICATE\n")
    result = propose_edit(tmp_workspace, f, "DUPLICATE", "REPLACED")
    # Two exact occurrences — should try line-exact or fuzzy
    assert result["ok"] is True
    assert result["match_tier"] in ("line_exact", "fuzzy")


def test_edit_file_not_found(tmp_workspace: Path):
    result = propose_edit(tmp_workspace, tmp_workspace / "nope.py", "a", "b")
    assert result["ok"] is False
    assert "file not found" in result["error"]


# ---------------------------------------------------------------------------
# propose_edit — Tier 2: Line-exact match
# ---------------------------------------------------------------------------

def test_edit_line_exact_match(sample_py_file: Path, tmp_workspace: Path):
    """Replace a full line block via line-exact matching."""
    old_str = "def goodbye():\n    print('goodbye world')"
    new_str = "def farewell():\n    print('farewell world')"
    result = propose_edit(tmp_workspace, sample_py_file, old_str, new_str)
    assert result["ok"] is True
    assert result["match_tier"] == "line_exact"
    assert "farewell" in result["new_content"]
    assert "goodbye" not in result["new_content"]


def test_edit_line_exact_multiple_identical_lines(tmp_workspace: Path):
    """If a line block appears multiple times, line-exact returns ambiguous —
    should fall through to fuzzy."""
    f = tmp_workspace / "repeat.py"
    f.write_text("---\nblock\n---\nblock\n---\n")
    result = propose_edit(tmp_workspace, f, "block", "REPLACED")
    # The line "block" appears twice — line-exact finds 2 matches, falls to fuzzy
    assert result["ok"] is True
    assert result["match_tier"] == "fuzzy"


def test_edit_line_exact_single_line(sample_py_file: Path, tmp_workspace: Path):
    """A single unique line should match via line-exact."""
    old_str = "class Greeter:"
    new_str = "class AdvancedGreeter:"
    result = propose_edit(tmp_workspace, sample_py_file, old_str, new_str)
    assert result["ok"] is True
    assert result["match_tier"] == "line_exact"


# ---------------------------------------------------------------------------
# propose_edit — Tier 3: Fuzzy whitespace-agnostic match
# ---------------------------------------------------------------------------

def test_edit_fuzzy_indentation_change(tmp_workspace: Path):
    """Fuzzy matching should handle indentation differences."""
    f = tmp_workspace / "indent.py"
    f.write_text("def foo():\n    pass\n")
    # old_str has extra leading whitespace
    old_str = "  def foo():\n      pass"
    new_str = "def bar():\n    return 42"
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is True
    assert result["match_tier"] == "fuzzy"
    assert "def bar()" in result["new_content"]
    assert "def foo()" not in result["new_content"]


def test_edit_fuzzy_small_typo(tmp_workspace: Path):
    """Fuzzy matching should handle small typos/discrepancies."""
    f = tmp_workspace / "typo.py"
    f.write_text("print('hello world')\n")
    old_str = "print('hello word')"  # missing 'l'
    new_str = "print('goodbye')"
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is True
    assert result["match_tier"] == "fuzzy"
    assert "goodbye" in result["new_content"]


def test_edit_fuzzy_below_threshold_fails(tmp_workspace: Path):
    """If the fuzzy ratio is below 0.75, the edit should fail."""
    f = tmp_workspace / "unrelated.py"
    f.write_text("completely different content here\n")
    old_str = "this does not appear at all anywhere"
    new_str = "replacement"
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_edit_empty_old_str(tmp_workspace: Path):
    """Empty old_str should fail cleanly."""
    f = tmp_workspace / "content.py"
    f.write_text("some content\n")
    result = propose_edit(tmp_workspace, f, "", "replacement")
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# propose_edit — edge cases
# ---------------------------------------------------------------------------

def test_edit_old_str_longer_than_file(tmp_workspace: Path):
    """If old_str has more lines than the file, fuzzy matching handles it gracefully."""
    f = tmp_workspace / "short.py"
    f.write_text("one line\n")
    result = propose_edit(tmp_workspace, f, "line1\nline2\nline3\nline4\nline5\n", "x")
    assert result["ok"] is False


def test_edit_replacement_is_exact(tmp_workspace: Path):
    """Verify the replacement content is placed exactly, with correct line endings."""
    f = tmp_workspace / "exact.py"
    original = "first\nsecond\nthird\nfourth\n"
    f.write_text(original)
    old_str = "second\nthird"
    new_str = "2nd\n3rd"
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is True
    assert result["new_content"] == "first\n2nd\n3rd\nfourth\n"
