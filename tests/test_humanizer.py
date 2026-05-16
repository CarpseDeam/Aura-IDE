from pathlib import Path

import pytest

from aura.humanizer import (
    HumanizerPipeline,
    HumanizerResult,
    is_valid_python,
    remove_ai_filler_comments,
    remove_internal_docstrings,
    strip_markdown_wrapper,
)


class TestStripMarkdownWrapper:
    def test_strip_single_fenced_block(self):
        code = """```python
def hello():
    print("Hello, world!")
```"""
        result, changed = strip_markdown_wrapper(code)
        assert changed is True
        assert result == 'def hello():\n    print("Hello, world!")'

    def test_strip_generic_fence(self):
        code = """```
some code here
```"""
        result, changed = strip_markdown_wrapper(code)
        assert changed is True
        assert result == "some code here"

    def test_preserve_multi_block(self):
        code = """```python
a = 1
```
```python
b = 2
```"""
        result, changed = strip_markdown_wrapper(code)
        assert changed is False
        assert result == code

    def test_preserve_unified_diff(self):
        code = """```python
@@ -1,3 +1,4 @@
 --- a/file.py
 +++ b/file.py
 def foo():
-    pass
+    return 1
```"""
        result, changed = strip_markdown_wrapper(code)
        assert changed is False
        assert result == code

    def test_preserve_no_fence(self):
        code = "def foo():\n    pass"
        result, changed = strip_markdown_wrapper(code)
        assert changed is False
        assert result == code


class TestRemoveAiFillerComments:
    def test_remove_filler_comments(self):
        code = """# Initialize the list
items = []
# Loop through items
for i in items:
    # Process each item
    print(i)
"""
        result, count = remove_ai_filler_comments(code)
        assert count == 3
        assert "# Initialize the list" not in result
        assert "# Loop through items" not in result
        assert "# Process each item" not in result
        assert "items = []" in result
        assert "for i in items:" in result

    def test_preserve_noqa_type_ignore(self):
        code = """x = 1  # noqa
y = 2  # type: ignore
z = 3  # pyright: ignore
"""
        result, count = remove_ai_filler_comments(code)
        assert count == 0
        assert result == code

    def test_preserve_todo_fixme(self):
        code = """# TODO: refactor this
# FIXME: bug here
# NOTE: important
# WARNING: fragile
# HACK: workaround
a = 1
"""
        result, count = remove_ai_filler_comments(code)
        assert count == 0
        assert result == code

    def test_preserve_urls(self):
        code = """# See https://example.com for details
# http://localhost:8000
x = 1
"""
        result, count = remove_ai_filler_comments(code)
        assert count == 0
        assert result == code

    def test_preserve_license_header(self):
        code = """# Copyright 2024 Acme Corp
# License: MIT
# Author: Jane Doe
# All rights reserved.
x = 1
"""
        result, count = remove_ai_filler_comments(code)
        assert count == 0
        assert result == code

    def test_preserve_inline_comments(self):
        code = """x = 1  # Initialize x with value
y = x + 1  # Calculate sum
"""
        result, count = remove_ai_filler_comments(code)
        assert count == 0
        assert "x = 1  # Initialize x with value" in result
        assert "y = x + 1  # Calculate sum" in result


class TestRemoveInternalDocstrings:
    def test_remove_useless_private_docstrings(self):
        code = """def _helper():
    \"\"\"Do the thing.\"\"\"
    return 42
"""
        result, count = remove_internal_docstrings(code)
        assert count == 1
        assert '"""Do the thing."""' not in result

    def test_keep_meaningful_public_docs(self):
        code = """def compute_mean(data):
    \"\"\"Calculate the arithmetic mean.

    Args:
        data: List of numbers.

    Returns:
        The mean as a float.
    \"\"\"
    return sum(data) / len(data)
"""
        result, count = remove_internal_docstrings(code)
        assert count == 0
        assert result == code

    def test_keep_dunder_docstrings(self):
        code = """class MyClass:
    def __init__(self, x: int):
        \"\"\"Initialize with x.\"\"\"
        self.x = x
"""
        result, count = remove_internal_docstrings(code)
        assert count == 0
        assert result == code


class TestHumanizerPipeline:
    def test_syntax_fallback(self):
        code = "def _bad():\n    \"\"\"Docstring.\"\"\""
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="python")
        assert result.syntax_fallback is True
        assert result.text == code

    def test_non_python_skips(self):
        code = "function hello() { return 1; }"
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="javascript")
        assert result.changed is False
        assert result.text == code

    def test_invalid_python_returns_original(self):
        code = "this is not valid python @@"
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="python")
        assert result.error is not None
        assert result.text == code

    def test_full_pipeline_humanizes(self):
        code = """```python
# Initialize the result
result = []

# Loop through the items
for i in range(10):
    # Process each item
    result.append(i)

# Return the result
result = result
```"""
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="python")
        assert result.markdown_stripped is True
        assert result.comments_removed >= 3
        assert result.changed is True
        assert "```" not in result.text
        assert "# Initialize the result" not in result.text
        assert "# Loop through the items" not in result.text
        assert "# Process each item" not in result.text
        assert "# Return the result" not in result.text
        assert "result = []" in result.text
        assert "for i in range(10):" in result.text
        assert "result.append(i)" in result.text
        assert "result = result" in result.text

    def test_import_from_aura_humanizer(self):
        from aura.humanizer import HumanizerPipeline as HP

        assert HP is HumanizerPipeline

    def test_is_valid_python(self):
        assert is_valid_python("x = 1") is True
        assert is_valid_python("def foo(): pass") is True
        assert is_valid_python("this is @@ invalid") is False
        assert is_valid_python("") is True

    def test_result_dataclass(self):
        r = HumanizerResult(path=Path("test.py"), language="python", original="a", text="b")
        assert r.path == Path("test.py")
        assert r.language == "python"
        assert r.original == "a"
        assert r.text == "b"
        assert r.changed is False
        assert r.elapsed_ms == 0.0


class TestHumanizerWriteIntegration:
    """Tests for the humanizer's behavior when integrated with write_file proposals."""

    def test_humanizer_changes_new_python_content(self):
        code = """```python
# Initialize the list
items = []
# Loop through items
for i in items:
    # Process each item
    print(i)
```"""
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="python")
        assert result.changed is True
        assert result.markdown_stripped is True
        assert result.comments_removed >= 3
        assert "```" not in result.text
        assert "# Initialize the list" not in result.text
        assert "# Loop through items" not in result.text
        assert "# Process each item" not in result.text

    def test_non_python_file_unchanged(self):
        code = "// This is a JavaScript comment\nfunction hello() { return 1; }"
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="javascript")
        assert result.changed is False
        assert result.text == code

    def test_syntax_fallback_returns_original(self):
        code = "this is not valid python syntax @@"
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="python")
        assert result.syntax_fallback is True
        assert result.text == code

    def test_humanizer_error_returns_original(self):
        """When result.error is set, result.text equals the original."""
        code = "this is not valid python @@"
        pipeline = HumanizerPipeline()
        result = pipeline.humanize_code(code, language="python")
        assert result.error is not None
        assert result.text == code


class TestDocstringRemovalPreservesFormatting:
    """Verify that removing internal docstrings preserves code structure."""

    def test_blank_lines_before_docstring_preserved(self):
        code = """
def _helper():

    \"\"\"Do the thing.\"\"\"
    return 42
"""
        result, count = remove_internal_docstrings(code)
        assert count == 1
        # A blank line before the docstring should remain
        assert '"""Do the thing."""' not in result
        # The blank line that was before the docstring should still be there
        lines = result.splitlines()
        blank_idx = lines.index("") if "" in lines else -1
        assert blank_idx >= 0

    def test_comments_near_docstrings_preserved(self):
        code = """
def _helper():
    # Important setup comment
    \"\"\"Do the thing.\"\"\"
    # Process data
    return 42
"""
        result, count = remove_internal_docstrings(code)
        assert count == 1
        assert "# Important setup comment" in result
        assert "# Process data" in result
        assert '"""Do the thing."""' not in result
        assert "return 42" in result

    def test_decorators_preserved(self):
        code = """
@staticmethod
def _helper():
    \"\"\"Do the thing.\"\"\"
    return 42
"""
        result, count = remove_internal_docstrings(code)
        assert count == 1
        assert "@staticmethod" in result
        assert '"""Do the thing."""' not in result
        assert "return 42" in result

    def test_inline_comments_survive(self):
        code = """
def _helper():
    \"\"\"Do the thing.\"\"\"
    x = 42  # The answer
    return x  # Return it
"""
        result, count = remove_internal_docstrings(code)
        assert count == 1
        assert '"""Do the thing."""' not in result
        assert "# The answer" in result
        assert "# Return it" in result


class TestNoAstUnparseInHumanizer:
    """Verify that aura/humanizer/ never uses ast.unparse for code generation."""

    def test_no_ast_unparse_in_humanizer_modules(self):
        import os

        import aura.humanizer

        humanizer_dir = os.path.dirname(aura.humanizer.__file__)
        found = []
        for fname in os.listdir(humanizer_dir):
            if fname.endswith(".py"):
                path = os.path.join(humanizer_dir, fname)
                with open(path, encoding="utf-8") as f:
                    source = f.read()
                if "ast.unparse" in source:
                    found.append(fname)
        assert not found, f"Modules using ast.unparse: {found}"
