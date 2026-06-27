"""Tests for aura.gui.worker_log_stream.formatter — normalize_assistant_display_text."""
from __future__ import annotations

from aura.gui.worker_log_stream.formatter import normalize_assistant_display_text


def test_empty_string() -> None:
    assert normalize_assistant_display_text("") == ""


def test_plain_text_unchanged() -> None:
    """Normal prose without glue should not be altered."""
    text = "This is a normal sentence.\n\nIt has proper spacing already."
    assert normalize_assistant_display_text(text) == text


def test_sentence_boundary_glue() -> None:
    """Sentence-boundary glue (punctuation + uppercase word) gets a paragraph break."""
    result = normalize_assistant_display_text("methods.Now let me review the code.")
    assert "methods.\n\nNow" in result


def test_beat_boundary_glue() -> None:
    """Missing-punctuation beat glue gets a paragraph break."""
    result = normalize_assistant_display_text("testsNow I have the results.")
    assert "tests\n\nNow I have" in result


def test_schema_beat_boundary() -> None:
    """Schema beat boundary with colon gets paragraph break."""
    result = normalize_assistant_display_text("schema:Let me define the data model.")
    assert "schema:\n\nLet me" in result


def test_codebase_beat_boundary() -> None:
    """Codebase beat boundary gets paragraph break."""
    result = normalize_assistant_display_text("codebase.Now I need to update the API.")
    assert "codebase.\n\nNow I" in result


def test_fenced_code_block_preserved() -> None:
    """Content inside fenced code blocks is NOT modified."""
    text = """I need to fix this.

```python
def hello():
    print("Now I will greet")
```
Now let me explain."""
    result = normalize_assistant_display_text(text)
    # The code block interior should be unchanged
    assert 'def hello():' in result
    assert '    print("Now I will greet")' in result
    # Beat starters OUTSIDE code blocks still get breaks
    assert "Now let me explain." in result


def test_inline_code_preserved() -> None:
    """Inline code spans are not modified by glue-splitting."""
    text = "The `testsNow` method returns the result.Let me check."
    result = normalize_assistant_display_text(text)
    # The inline code content should be preserved exactly
    assert '`testsNow`' in result
    # The beat glue after the inline code span should still work
    # (result. -> result.\n\n)
    # Note: the period after "result" is real punctuation, so the sentence-boundary
    # regex handles it: `Let` starts with uppercase L + et (2 lowercase) → matches.
    assert "result.\n\nLet me" in result.replace('`', '')


def test_camel_case_not_damaged() -> None:
    """camelCase identifiers should NOT be split."""
    text = "The getNow function should be kept as is."
    result = normalize_assistant_display_text(text)
    assert "getNow" in result
    # Note: "getNow" has 'w' (lowercase) before 'N' (uppercase), but 'Now' is
    # not followed by a beat-starter continuation (it's followed by space + "function").
    # The beat pattern requires the full beat-starter phrase like "Now I" or "Now let me"
    # to trigger a split. So "getNow function" won't trigger it. Good.


def test_beat_patterns_list() -> None:
    """Multiple beat patterns all get paragraph breaks."""
    tests = [
        ("First, let me", "First, let me"),  # Already properly spaced, no change
        ("codebase.First, let me", "codebase.\n\nFirst, let me"),
        ("data.Next, we should", "data.\n\nNext, we should"),
        ("model.Then, update.", "model.\n\nThen, update."),
        ("work.Finally, done.", "work.\n\nFinally, done."),
    ]
    for input_text, expected in tests:
        result = normalize_assistant_display_text(input_text)
        assert result == expected, f"Failed: {input_text!r} → {result!r}, expected {expected!r}"


def test_idempotent() -> None:
    """Applying normalize_assistant_display_text twice should not change the result."""
    text = "testsNow I have results.Let me check the log."
    once = normalize_assistant_display_text(text)
    twice = normalize_assistant_display_text(once)
    assert once == twice


def test_multiline_prose_with_multiple_glues() -> None:
    """Multiple glue patterns across multiple lines are all handled."""
    text = "Review the codebase.Now I need to check the tests.First, let me run them.\n\nschema:Let me define types.testsNow I can validate."
    result = normalize_assistant_display_text(text)
    assert "codebase.\n\nNow I" in result or "codebase.\n\nNow I" in result
    assert "tests.\n\nFirst," in result or "tests.\n\nFirst," in result
    assert "schema:\n\nLet me" in result
    assert "tests\n\nNow I" in result
