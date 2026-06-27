"""Tests for aura.conversation.tools.fs_write — the 3-tier edit matching algorithm."""

from __future__ import annotations

from pathlib import Path
from aura.conversation.tools.fs_edit_structured import propose_edit_symbol
from aura.conversation.tools.fs_write import propose_write, propose_edit, propose_line_range_edit, replace_line_range, _sanitize_edit_strings


# replace_line_range

def test_replace_line_range_single_line():
    original = "line0\nline1\nline2\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = replace_line_range(original, lines_with_nl, 1, 2, "REPLACED\n")
    assert result == "line0\nREPLACED\nline2\n"


def test_replace_line_range_multi_line():
    original = "a\nb\nc\nd\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = replace_line_range(original, lines_with_nl, 1, 3, "X\nY\n")
    assert result == "a\nX\nY\nd\n"


def test_replace_line_range_start_of_file():
    original = "one\ntwo\nthree\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = replace_line_range(original, lines_with_nl, 0, 1, "FIRST\n")
    assert result == "FIRST\ntwo\nthree\n"


def test_replace_line_range_end_of_file():
    original = "one\ntwo\nthree\n"
    lines_with_nl = original.splitlines(keepends=True)
    result = replace_line_range(original, lines_with_nl, 2, 3, "LAST\n")
    assert result == "one\ntwo\nLAST\n"


def test_replace_line_range_no_trailing_newline():
    """Handle files that don't end with a newline."""
    original = "line0\nline1\nline2"  # no trailing newline on last line
    lines_with_nl = original.splitlines(keepends=True)
    result = replace_line_range(original, lines_with_nl, 1, 2, "CHANGED\n")
    assert result == "line0\nCHANGED\nline2"


def test_propose_line_range_insert_before_line(tmp_workspace: Path):
    target = tmp_workspace / "insert.py"
    target.write_bytes(b"one = 1\nthree = 3\n")

    result = propose_line_range_edit(tmp_workspace, target, 2, 2, "two = 2\n")

    assert result["ok"] is True
    assert result["start_line"] == 2
    assert result["end_line"] == 2
    assert result["new_content"] == "one = 1\ntwo = 2\nthree = 3\n"


def test_propose_line_range_append_at_eof(tmp_workspace: Path):
    target = tmp_workspace / "append.py"
    target.write_bytes(b"one = 1\ntwo = 2\n")

    result = propose_line_range_edit(tmp_workspace, target, 3, 3, "three = 3\n")

    assert result["ok"] is True
    assert result["start_line"] == 3
    assert result["end_line"] == 3
    assert result["new_content"] == "one = 1\ntwo = 2\nthree = 3\n"


def test_propose_line_range_rejects_wrong_expected_old_str(tmp_workspace: Path):
    target = tmp_workspace / "stale.py"
    original = "one = 1\ntwo = 2\nthree = 3\n"
    target.write_text(original, encoding="utf-8")

    result = propose_line_range_edit(
        tmp_workspace,
        target,
        2,
        3,
        "two = 22\n",
        expected_old_str="not the current line\n",
    )

    assert result["ok"] is False
    assert result["failure_class"] == "edit_mechanics_stale_line_range"
    assert target.read_text(encoding="utf-8") == original


def test_propose_line_range_expected_old_str_allows_insert_and_eof_append(tmp_workspace: Path):
    target = tmp_workspace / "insert_append.py"
    target.write_bytes(b"one = 1\nthree = 3\n")

    inserted = propose_line_range_edit(
        tmp_workspace,
        target,
        2,
        2,
        "two = 2\n",
        expected_old_str="ignored for insertions",
    )
    assert inserted["ok"] is True
    assert inserted["new_content"] == "one = 1\ntwo = 2\nthree = 3\n"

    target.write_bytes(b"one = 1\ntwo = 2\n")
    appended = propose_line_range_edit(
        tmp_workspace,
        target,
        3,
        3,
        "three = 3\n",
        expected_old_str="ignored for eof append",
    )
    assert appended["ok"] is True
    assert appended["new_content"] == "one = 1\ntwo = 2\nthree = 3\n"


def test_propose_line_range_preserves_crlf_with_lf_replacement(tmp_workspace: Path):
    target = tmp_workspace / "range_crlf.py"
    target.write_bytes(b"one = 1\r\ntwo = 2\r\nthree = 3\r\n")

    result = propose_line_range_edit(
        tmp_workspace,
        target,
        2,
        3,
        "two = 22\n",
        expected_old_str="two = 2\n",
    )

    assert result["ok"] is True
    assert result["new_content"] == "one = 1\r\ntwo = 22\r\nthree = 3\r\n"

# propose_write

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


# propose_edit — Tier 1: Exact string match

def test_edit_exact_match_unique(sample_py_file: Path, tmp_workspace: Path):
    """Single occurrence — should replace via exact match."""
    result = propose_edit(tmp_workspace, sample_py_file, "hello world", "HELLO WORLD")
    assert result["ok"] is True
    assert result["match_tier"] == "exact"
    assert "HELLO WORLD" in result["new_content"]
    assert "hello world" not in result["new_content"]


def test_edit_exact_duplicate_falls_through_to_ambiguous(tmp_workspace: Path):
    """When old_str appears multiple times, exact match falls through to fuzzy
    and should reject as ambiguous."""
    f = tmp_workspace / "dup.py"
    f.write_text("DUPLICATE\nmiddle\nDUPLICATE\n")
    result = propose_edit(tmp_workspace, f, "DUPLICATE", "REPLACED")
    # Two exact occurrences — line-exact finds 2 matches, fuzzy finds 2 at ratio 1.0
    assert result["ok"] is False
    assert "ambiguous" in result["error"]


def test_edit_file_not_found(tmp_workspace: Path):
    result = propose_edit(tmp_workspace, tmp_workspace / "nope.py", "a", "b")
    assert result["ok"] is False
    assert "file not found" in result["error"]


# propose_edit — Tier 2: Line-exact match

def test_edit_line_exact_match(sample_py_file: Path, tmp_workspace: Path):
    """Replace a full line block — matches exactly since it's a unique substring."""
    old_str = "def goodbye():\n    print('goodbye world')"
    new_str = "def farewell():\n    print('farewell world')"
    result = propose_edit(tmp_workspace, sample_py_file, old_str, new_str)
    assert result["ok"] is True
    # Unique substring → exact match (Tier 1) catches it first
    assert result["match_tier"] == "exact"
    assert "farewell" in result["new_content"]
    assert "goodbye" not in result["new_content"]


def test_edit_line_exact_multiple_identical_lines(tmp_workspace: Path):
    """If a line block appears multiple times, line-exact returns ambiguous —
    fuzzy should reject as ambiguous."""
    f = tmp_workspace / "repeat.py"
    f.write_text("---\nblock\n---\nblock\n---\n")
    result = propose_edit(tmp_workspace, f, "block", "REPLACED")
    # The line "block" appears twice — line-exact finds 2 matches, fuzzy finds 2
    # at ratio 1.0 → ambiguous
    assert result["ok"] is False
    assert "ambiguous" in result["error"]


def test_edit_line_exact_single_line(sample_py_file: Path, tmp_workspace: Path):
    """A single unique line — matches exactly since it's a unique substring."""
    old_str = "class Greeter:"
    new_str = "class AdvancedGreeter:"
    result = propose_edit(tmp_workspace, sample_py_file, old_str, new_str)
    assert result["ok"] is True
    # Unique substring → exact match (Tier 1) catches it first
    assert result["match_tier"] == "exact"


def test_edit_line_exact_when_exact_substring_is_not_unique(tmp_workspace: Path):
    f = tmp_workspace / "line_exact.py"
    f.write_bytes(b"target\nprefix target suffix\n")

    result = propose_edit(tmp_workspace, f, "target", "replacement")

    assert result["ok"] is True
    assert result["match_tier"] == "line_exact"
    assert result["new_content"] == "replacementprefix target suffix\n"


# propose_edit — Tier 3: Fuzzy whitespace-agnostic match

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


def test_edit_fuzzy_unique_with_competitor(tmp_workspace: Path):
    """Two blocks both match old_str above threshold, but one is clearly better."""
    f = tmp_workspace / "competitor.py"
    f.write_text(
        "def foo():\n"
        "  return 1\n"
        "\n"
        "def bar():\n"
        "    return 1\n"
    )
    old_str = "def foo():\n    return 1"
    new_str = "def foo():\n    return 2"
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is True
    assert result["match_tier"] == "fuzzy"
    assert "return 2" in result["new_content"]


def test_edit_fuzzy_ambiguous_two_blocks(tmp_workspace: Path):
    """Two blocks with identical normalized content — ambiguous fuzzy match."""
    f = tmp_workspace / "ambiguous2.py"
    f.write_text("common\nother\ncommon\n")
    old_str = "common"
    result = propose_edit(tmp_workspace, f, old_str, "REPLACED")
    assert result["ok"] is False
    assert "ambiguous" in result["error"]


def test_edit_fuzzy_ambiguous_three_blocks(tmp_workspace: Path):
    """Three blocks with identical normalized content — ambiguous fuzzy match."""
    f = tmp_workspace / "ambiguous3.py"
    f.write_text("a\nb\na\nb\na\n")
    old_str = "a"
    result = propose_edit(tmp_workspace, f, old_str, "REPLACED")
    assert result["ok"] is False
    assert "ambiguous" in result["error"]
    assert "3 blocks" in result["error"]


def test_edit_empty_old_str(tmp_workspace: Path):
    """Empty old_str should fail cleanly."""
    f = tmp_workspace / "content.py"
    f.write_text("some content\n")
    result = propose_edit(tmp_workspace, f, "", "replacement")
    assert result["ok"] is False


# propose_edit — edge cases

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
    f.write_bytes(original.encode("utf-8"))
    old_str = "second\nthird"
    new_str = "2nd\n3rd"
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is True
    assert result["new_content"] == "first\n2nd\n3rd\nfourth\n"


def test_edit_matches_lf_search_on_crlf_file_and_preserves_crlf(tmp_workspace: Path):
    f = tmp_workspace / "edit_crlf.py"
    f.write_bytes(b"first\r\nsecond\r\nthird\r\n")

    result = propose_edit(tmp_workspace, f, "second\n", "2nd\n")

    assert result["ok"] is True
    assert result["new_content"] == "first\r\n2nd\r\nthird\r\n"


def test_edit_preserves_lf_file(tmp_workspace: Path):
    f = tmp_workspace / "edit_lf.py"
    f.write_bytes(b"first\nsecond\nthird\n")

    result = propose_edit(tmp_workspace, f, "second\n", "2nd\n")

    assert result["ok"] is True
    assert result["new_content"] == "first\n2nd\nthird\n"


# _sanitize_edit_strings (tested via propose_edit)


def test_edit_strips_markdown_fence_python(sample_py_file: Path, tmp_workspace: Path):
    """old_str wrapped in ```python ... ``` should be stripped before matching."""
    old_str = "```python\ndef hello():\n    print('hello world')\n```"
    new_str = "def hello():\n    print('hola mundo')"
    result = propose_edit(tmp_workspace, sample_py_file, old_str, new_str)
    assert result["ok"] is True
    assert "hola mundo" in result["new_content"]


def test_edit_strips_markdown_fence_no_lang(sample_py_file: Path, tmp_workspace: Path):
    """old_str wrapped in ``` ... ``` (no language tag) should be stripped."""
    old_str = "```\ndef hello():\n    print('hello world')\n```"
    new_str = "def hello():\n    print('bonjour')"
    result = propose_edit(tmp_workspace, sample_py_file, old_str, new_str)
    assert result["ok"] is True
    assert "bonjour" in result["new_content"]


def test_edit_preserves_internal_code_blocks(tmp_workspace: Path):
    """Only outermost fences are stripped; internal ``` must stay."""
    f = tmp_workspace / "internal_fence.py"
    f.write_text("line1\nline2\nline3\n")
    # old_str has a fake internal ``` that should NOT be stripped
    old_str = "some text\n```\nmore text"
    new_str = "replaced"
    # This should just fall through to matching — the point is no crash/over-strip
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is False  # won't match, but shouldn't error from sanitizer


def test_edit_fence_with_trailing_newlines(tmp_workspace: Path):
    """Fence with trailing whitespace/newlines after closing ``` should be handled."""
    f = tmp_workspace / "trailing.py"
    f.write_text("def foo():\n    return 1\n")
    old_str = "```python\ndef foo():\n    return 1\n```\n"
    new_str = "def bar():\n    return 2"
    result = propose_edit(tmp_workspace, f, old_str, new_str)
    assert result["ok"] is True
    assert "def bar()" in result["new_content"]
    assert result.get("sanitized") is True


# propose_edit_symbol


def test_edit_symbol_replace_function(sample_py_file: Path, tmp_workspace: Path):
    result = propose_edit_symbol(
        tmp_workspace, sample_py_file, "function", "hello",
        "def hello():\n    print('new hello')",
    )
    assert result["ok"] is True
    assert result["match_tier"] == "symbol"
    assert "def hello():" in result["new_content"]
    assert "new hello" in result["new_content"]
    assert "hello world" not in result["new_content"]


def test_edit_symbol_replace_class(sample_py_file: Path, tmp_workspace: Path):
    result = propose_edit_symbol(
        tmp_workspace, sample_py_file, "class", "Greeter",
        "class Greeter:\n    def greet(self, name):\n        return f'Hey, {name}'",
    )
    assert result["ok"] is True
    assert result["match_tier"] == "symbol"
    assert "Hey" in result["new_content"]


def test_edit_symbol_replace_method(sample_py_file: Path, tmp_workspace: Path):
    result = propose_edit_symbol(
        tmp_workspace, sample_py_file, "method", "greet",
        "def greet(self, name):\n    return f'Yo, {name}'",
        class_name="Greeter",
    )
    assert result["ok"] is True
    assert result["match_tier"] == "symbol"
    assert "Yo" in result["new_content"]


def test_edit_symbol_not_found(sample_py_file: Path, tmp_workspace: Path):
    result = propose_edit_symbol(
        tmp_workspace, sample_py_file, "function", "nonexistent",
        "def foo(): pass",
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_edit_symbol_non_python_file(tmp_workspace: Path):
    f = tmp_workspace / "script.js"
    f.write_text("function hello() { console.log('hi'); }")
    result = propose_edit_symbol(
        tmp_workspace, f, "function", "hello", "function hello() {}",
    )
    assert result["ok"] is False
    assert "only supports python" in result["error"].lower()


def test_edit_symbol_decorated_function(tmp_workspace: Path):
    """Replace a decorated function — lineno should include decorator."""
    f = tmp_workspace / "deco.py"
    f.write_text("@app.route('/')\ndef index():\n    return 'hi'\n")
    result = propose_edit_symbol(
        tmp_workspace, f, "function", "index",
        "@app.route('/')\ndef index():\n    return 'hello'",
    )
    assert result["ok"] is True
    assert result["match_tier"] == "symbol"
    assert "hello" in result["new_content"]


def test_edit_symbol_function_as_method_with_class_name(sample_py_file: Path, tmp_workspace: Path):
    """symbol_type='function' with class_name should be treated as method."""
    result = propose_edit_symbol(
        tmp_workspace, sample_py_file, "function", "greet",
        "def greet(self, name):\n    return f'Yo, {name}'",
        class_name="Greeter",
    )
    assert result["ok"] is True
    assert result["match_tier"] == "symbol"
    assert "Yo" in result["new_content"]


def test_edit_symbol_invalid_replacement_returns_ok_false(sample_py_file: Path, tmp_workspace: Path):
    """Syntax-invalid replacement body should return ok=False with a clear error."""
    # '1+' is an incomplete expression — SyntaxError at parse time
    result = propose_edit_symbol(
        tmp_workspace, sample_py_file, "function", "hello",
        "def hello():\n    1+",
    )
    assert result["ok"] is False
    # Error message should mention the proposed replacement made the file invalid
    # and include the syntax error detail
    assert "invalid" in result["error"].lower() or "syntax" in result["error"].lower()
    assert result["new_content"] == ""
    # Original content is preserved in old_content
    assert "hello world" in result["old_content"]
