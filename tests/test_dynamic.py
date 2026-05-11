"""Comprehensive tests for aura.conversation.tools.dynamic.

Tests cover all five public/private functions in the module:
    _get_base_name
    _annotation_to_json_type
    _parse_docstring_args
    parse_tool_schema
    execute_dynamic_tool

Style follows the existing class-per-function convention in the test suite.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.conversation.tools.dynamic import (
    _annotation_to_json_type,
    _get_base_name,
    _parse_docstring_args,
    execute_dynamic_tool,
    parse_tool_schema,
)

# ===================================================================
# _get_base_name
# ===================================================================


class TestGetBaseName:
    """Tests for _get_base_name — extracts a readable name from an AST expr node."""

    def test_ast_name(self):
        """ast.Name("list") → "list"."""
        node = ast.Name(id="list")
        assert _get_base_name(node) == "list"

    def test_ast_attribute(self):
        """ast.Attribute(value=Name("typing"), attr="List") → "List"."""
        node = ast.Attribute(value=ast.Name(id="typing"), attr="List")
        assert _get_base_name(node) == "List"

    def test_ast_constant(self):
        """ast.Constant(value=None) → "" (not a Name or Attribute)."""
        node = ast.Constant(value=None)
        assert _get_base_name(node) == ""

    def test_ast_subscript(self):
        """ast.Subscript — fallback returns empty string."""
        node = ast.Subscript(value=ast.Name(id="list"), slice=ast.Name(id="str"))
        assert _get_base_name(node) == ""

    def test_ast_binop(self):
        """ast.BinOp — not Name or Attribute → "". """
        node = ast.BinOp(left=ast.Name(id="str"), op=ast.BitOr(), right=ast.Constant(value=None))
        assert _get_base_name(node) == ""


# ===================================================================
# _annotation_to_json_type
# ===================================================================


class TestAnnotationToJsonType:
    """Tests for _annotation_to_json_type — maps AST annotation nodes to JSON Schema type strings."""

    def test_none(self):
        """None → "string"."""
        assert _annotation_to_json_type(None) == "string"

    def test_str(self):
        """ast.Name("str") → "string"."""
        assert _annotation_to_json_type(ast.Name(id="str")) == "string"

    def test_int(self):
        """ast.Name("int") → "integer"."""
        assert _annotation_to_json_type(ast.Name(id="int")) == "integer"

    def test_float(self):
        """ast.Name("float") → "number"."""
        assert _annotation_to_json_type(ast.Name(id="float")) == "number"

    def test_bool(self):
        """ast.Name("bool") → "boolean"."""
        assert _annotation_to_json_type(ast.Name(id="bool")) == "boolean"

    def test_none_name(self):
        """ast.Name("None") → "null"."""
        assert _annotation_to_json_type(ast.Name(id="None")) == "null"

    def test_any(self):
        """ast.Name("Any") → "string" (fallback)."""
        assert _annotation_to_json_type(ast.Name(id="Any")) == "string"

    def test_unknown_type(self):
        """ast.Name("unknown_type") → "string" (fallback)."""
        assert _annotation_to_json_type(ast.Name(id="unknown_type")) == "string"

    # --- PEP 604 union tests ---

    def test_pep604_str_or_none(self):
        """str | None → "string" (first non-null side wins)."""
        node = ast.BinOp(
            left=ast.Name(id="str"),
            op=ast.BitOr(),
            right=ast.Constant(value=None),
        )
        assert _annotation_to_json_type(node) == "string"

    def test_pep604_none_or_int(self):
        """None | int → "integer" (left is null, right is int)."""
        node = ast.BinOp(
            left=ast.Constant(value=None),
            op=ast.BitOr(),
            right=ast.Name(id="int"),
        )
        assert _annotation_to_json_type(node) == "integer"

    def test_pep604_none_or_none(self):
        """None | None → "null"."""
        node = ast.BinOp(
            left=ast.Constant(value=None),
            op=ast.BitOr(),
            right=ast.Constant(value=None),
        )
        assert _annotation_to_json_type(node) == "null"

    def test_binop_non_bitor(self):
        """BinOp with non-BitOr operator → "string"."""
        node = ast.BinOp(
            left=ast.Name(id="str"),
            op=ast.Add(),
            right=ast.Name(id="int"),
        )
        assert _annotation_to_json_type(node) == "string"

    # --- Subscript tests ---

    def test_subscript_list(self):
        """ast.Subscript(ast.Name("list"), ...) → "array"."""
        node = ast.Subscript(value=ast.Name(id="list"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "array"

    def test_subscript_typing_list(self):
        """ast.Subscript(Attribute(typing, List), ...) → "array"."""
        node = ast.Subscript(
            value=ast.Attribute(value=ast.Name(id="typing"), attr="List"),
            slice=ast.Name(id="int"),
        )
        assert _annotation_to_json_type(node) == "array"

    def test_subscript_sequence(self):
        """ast.Subscript(Name("Sequence"), ...) → "array"."""
        node = ast.Subscript(value=ast.Name(id="Sequence"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "array"

    def test_subscript_mutablesequence(self):
        """ast.Subscript(Name("MutableSequence"), ...) → "array"."""
        node = ast.Subscript(value=ast.Name(id="MutableSequence"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "array"

    def test_subscript_dict(self):
        """ast.Subscript(ast.Name("dict"), ...) → "object"."""
        node = ast.Subscript(
            value=ast.Name(id="dict"),
            slice=ast.Tuple(elts=[ast.Name(id="str"), ast.Name(id="int")]),
        )
        assert _annotation_to_json_type(node) == "object"

    def test_subscript_typing_dict(self):
        """ast.Subscript(Attribute(typing, Dict), ...) → "object"."""
        node = ast.Subscript(
            value=ast.Attribute(value=ast.Name(id="typing"), attr="Dict"),
            slice=ast.Tuple(elts=[ast.Name(id="str"), ast.Name(id="int")]),
        )
        assert _annotation_to_json_type(node) == "object"

    def test_subscript_mapping(self):
        """ast.Subscript(Name("Mapping"), ...) → "object"."""
        node = ast.Subscript(value=ast.Name(id="Mapping"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "object"

    def test_subscript_mutablemapping(self):
        """ast.Subscript(Name("MutableMapping"), ...) → "object"."""
        node = ast.Subscript(value=ast.Name(id="MutableMapping"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "object"

    def test_subscript_optional(self):
        """ast.Subscript(ast.Name("Optional"), ast.Name("str")) → "string" (unwraps Optional)."""
        node = ast.Subscript(value=ast.Name(id="Optional"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "string"

    def test_subscript_union(self):
        """ast.Subscript(ast.Name("Union"), ...) → "string" (fallback)."""
        node = ast.Subscript(
            value=ast.Name(id="Union"),
            slice=ast.Tuple(elts=[ast.Name(id="str"), ast.Name(id="int")]),
        )
        assert _annotation_to_json_type(node) == "string"

    # --- Constant tests ---

    def test_constant_none(self):
        """ast.Constant(value=None) → "null"."""
        node = ast.Constant(value=None)
        assert _annotation_to_json_type(node) == "null"

    def test_constant_non_none(self):
        """ast.Constant(value=123) → "string" (non-None constant)."""
        node = ast.Constant(value=123)
        assert _annotation_to_json_type(node) == "string"

    # --- Edge cases for completeness ---

    def test_tuple_subscript(self):
        """Subscript with unknown base (e.g., Tuple) → "string"."""
        node = ast.Subscript(value=ast.Name(id="Tuple"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "string"

    def test_set_subscript(self):
        """Subscript with unknown base (e.g., Set) → "string"."""
        node = ast.Subscript(value=ast.Name(id="Set"), slice=ast.Name(id="str"))
        assert _annotation_to_json_type(node) == "string"

    def test_fallback_unknown_node_type(self):
        """A completely unrecognised AST node type → "string" (conservative fallback).

        This hits the final ``return "string"`` at the end of
        _annotation_to_json_type, reached when ann_node is not None, BinOp,
        Constant, Name, or Subscript.
        """
        # ast.List is not any of the checked types
        node = ast.List(elts=[])
        assert _annotation_to_json_type(node) == "string"

    def test_subscript_optional_list(self):
        """Optional[list[str]] → "array" (unwraps Optional, then sees list)."""
        node = ast.Subscript(
            value=ast.Name(id="Optional"),
            slice=ast.Subscript(value=ast.Name(id="list"), slice=ast.Name(id="str")),
        )
        assert _annotation_to_json_type(node) == "array"

    def test_subscript_list_of_lists(self):
        """list[list[str]] → "array"."""
        node = ast.Subscript(
            value=ast.Name(id="list"),
            slice=ast.Subscript(value=ast.Name(id="list"), slice=ast.Name(id="str")),
        )
        assert _annotation_to_json_type(node) == "array"

    def test_subscript_dict_complex_value(self):
        """dict[str, list[int]] → "object"."""
        node = ast.Subscript(
            value=ast.Name(id="dict"),
            slice=ast.Tuple(
                elts=[
                    ast.Name(id="str"),
                    ast.Subscript(value=ast.Name(id="list"), slice=ast.Name(id="int")),
                ]
            ),
        )
        assert _annotation_to_json_type(node) == "object"


# ===================================================================
# _parse_docstring_args
# ===================================================================


class TestParseDocstringArgs:
    """Tests for _parse_docstring_args — extracts Args block from docstrings."""

    def test_none(self):
        """None → {}."""
        assert _parse_docstring_args(None) == {}

    def test_empty_string(self):
        """Empty string → {}."""
        assert _parse_docstring_args("") == {}

    def test_no_args_block(self):
        """Docstring with no Args: block → {}."""
        doc = "This function does something.\n\nReturns:\n    True."
        assert _parse_docstring_args(doc) == {}

    def test_single_param(self):
        """Docstring with Args: containing one parameter."""
        doc = """Do something.

Args:
    name: The name to use.
"""
        result = _parse_docstring_args(doc)
        assert result == {"name": "The name to use."}

    def test_multiple_params(self):
        """Docstring with Args: containing multiple parameters."""
        doc = """Do something.

Args:
    name: The name to use.
    age: The age of the person.
    active: Whether active.
"""
        result = _parse_docstring_args(doc)
        assert result == {
            "name": "The name to use.",
            "age": "The age of the person.",
            "active": "Whether active.",
        }

    def test_stops_at_next_section(self):
        """Args: block followed by another section stops at the next section."""
        doc = """Do something.

Args:
    name: The name.

Returns:
    A string.
"""
        result = _parse_docstring_args(doc)
        assert result == {"name": "The name."}

    def test_skips_malformed_line(self):
        """Malformed line (no colon) is skipped."""
        doc = """Do something.

Args:
    name: The name.
    bad_line_no_colon
    age: The age.
"""
        result = _parse_docstring_args(doc)
        assert result == {"name": "The name.", "age": "The age."}

    def test_empty_description(self):
        """Parameter with empty description after colon → empty string."""
        doc = """Do something.

Args:
    name:
"""
        result = _parse_docstring_args(doc)
        assert result == {"name": ""}

    def test_blank_lines_in_args_block(self):
        """Blank lines inside the Args block are skipped, not treated as section end."""
        doc = """Do something.

Args:
    name: The name.

    age: The age.
"""
        result = _parse_docstring_args(doc)
        # Blank line is skipped, then the next indented line is still part of Args.
        # "age" is indented but the blank line means it's still parsed.
        # Actually the blank line stripped is "", and then "    age: The age." starts with " " so it continues.
        assert result == {"name": "The name.", "age": "The age."}

    def test_not_indented_ends_args(self):
        """A non-empty, non-indented line after Args starts a new section."""
        doc = """Do something.

Args:
    name: The name.
Some other section:
    foo: bar
"""
        result = _parse_docstring_args(doc)
        assert result == {"name": "The name."}

    def test_colon_in_description(self):
        """Description text containing colons is handled correctly (partition on first colon only)."""
        doc = """Do something.

Args:
    time: The format is HH:MM:SS.
"""
        result = _parse_docstring_args(doc)
        assert result == {"time": "The format is HH:MM:SS."}

    def test_tab_indented_args(self):
        """Tab characters instead of spaces for indentation in Args block."""
        doc = """Do something.

Args:
\tname: The name to use.
\tage: The age.
"""
        result = _parse_docstring_args(doc)
        assert result == {"name": "The name to use.", "age": "The age."}

    def test_multi_word_param_name(self):
        """Parameter name with underscores (e.g. user_name) is parsed correctly."""
        doc = """Do something.

Args:
    user_name: The user's display name.
"""
        result = _parse_docstring_args(doc)
        assert result == {"user_name": "The user's display name."}


# ===================================================================
# parse_tool_schema
# ===================================================================


class TestParseToolSchema:
    """Tests for parse_tool_schema — parses .py files into OpenAI tool definitions."""

    # -- Happy paths -------------------------------------------------------

    def test_simple_function(self, tmp_path: Path):
        """A simple typed function yields name, description, params, required."""
        src = (
            "def greet(name: str, age: int) -> str:\n"
            '    """Say hello to someone.\n\n'
            "    Args:\n"
            "        name: The person's name.\n"
            "        age: How old they are.\n"
            '    """\n'
            "    return f'Hello {name}'\n"
        )
        fp = tmp_path / "greet.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "greet"
        assert schema["function"]["description"] == "Say hello to someone."
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert params["properties"]["name"]["type"] == "string"
        assert params["properties"]["name"]["description"] == "The person's name."
        assert params["properties"]["age"]["type"] == "integer"
        assert params["properties"]["age"]["description"] == "How old they are."
        assert params["required"] == ["name", "age"]

    def test_no_annotation_defaults_to_string(self, tmp_path: Path):
        """Parameters without annotations default to "string" type."""
        src = "def foo(name, value): pass\n"
        fp = tmp_path / "no_ann.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["name"]["type"] == "string"
        assert params["properties"]["value"]["type"] == "string"

    def test_default_values_not_required(self, tmp_path: Path):
        """Parameters with default values are NOT in required."""
        src = (
            "def configure(host: str, port: int = 8080, debug: bool = False): pass\n"
        )
        fp = tmp_path / "configure.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        required = schema["function"]["parameters"]["required"]
        assert required == ["host"]
        assert "port" not in required
        assert "debug" not in required

    def test_vararg_not_required(self, tmp_path: Path):
        """*args appears as array property and is NOT required."""
        src = (
            "def log(*messages: str) -> None:\n"
            '    """Log messages.\n\n'
            "    Args:\n"
            "        messages: The messages to log.\n"
            '    """\n'
            "    pass\n"
        )
        fp = tmp_path / "log.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["messages"]["type"] == "array"
        assert params["properties"]["messages"]["description"] == "The messages to log."
        assert "messages" not in params["required"]

    def test_kwarg_not_required(self, tmp_path: Path):
        """**kwargs appears as object property and is NOT required."""
        src = (
            "def format(**options: bool) -> None:\n"
            '    """Format with options.\n\n'
            "    Args:\n"
            "        options: Formatting options.\n"
            '    """\n'
            "    pass\n"
        )
        fp = tmp_path / "format.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["options"]["type"] == "object"
        assert params["properties"]["options"]["description"] == "Formatting options."
        assert "options" not in params["required"]

    def test_async_function(self, tmp_path: Path):
        """Async functions are found and parsed correctly."""
        src = (
            "async def fetch(url: str) -> str:\n"
            '    """Fetch a URL."""\n'
            "    return 'data'\n"
        )
        fp = tmp_path / "fetch.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        assert schema["function"]["name"] == "fetch"
        assert schema["function"]["description"] == "Fetch a URL."
        assert schema["function"]["parameters"]["properties"]["url"]["type"] == "string"
        assert schema["function"]["parameters"]["required"] == ["url"]

    def test_docstring_description_first_paragraph(self, tmp_path: Path):
        """The first paragraph of the docstring becomes the description."""
        src = (
            "def test_func() -> None:\n"
            '    """First paragraph.\n\n'
            "    Second paragraph with more details.\n"
            '    """\n'
            "    pass\n"
        )
        fp = tmp_path / "test_func.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        assert schema["function"]["description"] == "First paragraph."

    def test_no_docstring_fallback(self, tmp_path: Path):
        """No docstring → description is \"Dynamic tool: {name}\"."""
        src = "def my_tool(x: int) -> int: return x\n"
        fp = tmp_path / "my_tool.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        assert schema["function"]["description"] == "Dynamic tool: my_tool"

    def test_args_descriptions_populated(self, tmp_path: Path):
        """Args descriptions from docstring populate the properties."""
        src = (
            "def search(query: str, limit: int = 10) -> list:\n"
            '    """Search for items.\n\n'
            "    Args:\n"
            "        query: The search query string.\n"
            "        limit: Max number of results.\n"
            '    """\n'
            "    return []\n"
        )
        fp = tmp_path / "search.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        props = schema["function"]["parameters"]["properties"]
        assert props["query"]["description"] == "The search query string."
        assert props["limit"]["description"] == "Max number of results."

    # -- Error paths -------------------------------------------------------

    def test_non_existent_file(self, tmp_path: Path):
        """Non-existent file → FileNotFoundError."""
        fp = tmp_path / "nonexistent.py"
        with pytest.raises(FileNotFoundError):
            parse_tool_schema(fp)

    def test_syntax_error(self, tmp_path: Path):
        """File with syntax error → ValueError with \"Syntax error\" message."""
        src = "def broken(: int\n"  # deliberate syntax error
        fp = tmp_path / "broken.py"
        fp.write_text(src, encoding="utf-8")
        with pytest.raises(ValueError, match="Syntax error"):
            parse_tool_schema(fp)

    def test_no_top_level_function(self, tmp_path: Path):
        """File with no top-level function → ValueError."""
        src = (
            "import os\n"
            "class MyClass:\n"
            "    def method(self): pass\n"
            "x = 42\n"
        )
        fp = tmp_path / "no_func.py"
        fp.write_text(src, encoding="utf-8")
        with pytest.raises(ValueError, match="No top-level function"):
            parse_tool_schema(fp)

    def test_only_async_gen_no_func(self, tmp_path: Path):
        """File with only a class, imports, but no function raises error."""
        src = (
            "from typing import Any\n"
            "\n"
            "CONSTANT = 42\n"
        )
        fp = tmp_path / "constants.py"
        fp.write_text(src, encoding="utf-8")
        with pytest.raises(ValueError, match="No top-level function"):
            parse_tool_schema(fp)

    def test_optional_list_annotation(self, tmp_path: Path):
        """Optional[list[str]] annotation maps to type "array"."""
        src = (
            "from typing import Optional\n"
            "\n"
            "def process(data: Optional[list[str]]) -> None:\n"
            '    """Process data.\n\n'
            "    Args:\n"
            "        data: The data to process.\n"
            '    """\n'
            "    pass\n"
        )
        fp = tmp_path / "process.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["data"]["type"] == "array"

    def test_list_of_lists_annotation(self, tmp_path: Path):
        """list[list[float]] annotation maps to type "array"."""
        src = (
            "def transform(matrix: list[list[float]]) -> None:\n"
            '    """Transform the matrix."""\n'
            "    pass\n"
        )
        fp = tmp_path / "transform.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["matrix"]["type"] == "array"

    def test_dict_with_complex_value_annotation(self, tmp_path: Path):
        """dict[str, list[int]] annotation maps to type "object"."""
        src = (
            "def lookup(mapping: dict[str, list[int]]) -> None:\n"
            '    """Look up values."""\n'
            "    pass\n"
        )
        fp = tmp_path / "lookup.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["mapping"]["type"] == "object"

    def test_pep604_union_annotation(self, tmp_path: Path):
        """PEP 604 str | None annotation → type "string", not in required (has default)."""
        src = (
            "def greet(name: str | None = None) -> str:\n"
            '    """Greet someone.\n\n'
            "    Args:\n"
            "        name: The person's name.\n"
            '    """\n'
            "    return f'Hello {name}'\n"
        )
        fp = tmp_path / "greet.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["name"]["type"] == "string"
        assert "name" not in params["required"]

    def test_float_default(self, tmp_path: Path):
        """Parameter with float default → type "number", not in required."""
        src = (
            "def scale(ratio: float = 0.5) -> float:\n"
            '    """Scale by ratio."""\n'
            "    return ratio\n"
        )
        fp = tmp_path / "scale.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"]["ratio"]["type"] == "number"
        assert "ratio" not in params["required"]

    def test_all_default_types(self, tmp_path: Path):
        """Mixed required/optional params: only param without default is required."""
        src = (
            "def config(host: str, port: int = 8080, debug: bool = False, ratio: float = 1.0) -> str:\n"
            '    """Configure the service."""\n'
            "    return host\n"
        )
        fp = tmp_path / "config.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["required"] == ["host"]
        assert params["properties"]["port"]["type"] == "integer"
        assert params["properties"]["debug"]["type"] == "boolean"
        assert params["properties"]["ratio"]["type"] == "number"

    def test_kwonly_args_omitted(self, tmp_path: Path):
        """Keyword-only args (after *) are parsed correctly."""
        src = (
            "def format(*, indent: int = 2, color: bool = True) -> str:\n"
            '    """Format output."""\n'
            "    return ''\n"
        )
        fp = tmp_path / "format.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        # Both kwonlyargs should appear in properties
        assert "indent" in params["properties"]
        assert "color" in params["properties"]
        assert params["properties"]["indent"]["type"] == "integer"
        assert params["properties"]["color"]["type"] == "boolean"
        # Both have defaults, so neither should be required
        assert "indent" not in params["required"]
        assert "color" not in params["required"]

    def test_kwonly_arg_required_when_no_default(self, tmp_path: Path):
        """Keyword-only arg without a default is included in required."""
        src = (
            "def render(*, title: str, width: int = 800) -> None:\n"
            '    """Render content."""\n'
            "    pass\n"
        )
        fp = tmp_path / "render.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        # Both appear in properties
        assert "title" in params["properties"]
        assert "width" in params["properties"]
        assert params["properties"]["title"]["type"] == "string"
        assert params["properties"]["width"]["type"] == "integer"
        # Only title (no default) is required; width (has default) is not
        assert "title" in params["required"]
        assert "width" not in params["required"]

    def test_empty_args_list(self, tmp_path: Path):
        """Function with no parameters → empty properties and required."""
        src = (
            "def ping() -> str:\n"
            '    """Check if alive."""\n'
            "    return 'pong'\n"
        )
        fp = tmp_path / "ping.py"
        fp.write_text(src, encoding="utf-8")
        schema = parse_tool_schema(fp)
        params = schema["function"]["parameters"]
        assert params["properties"] == {}
        assert params["required"] == []


# ===================================================================
# execute_dynamic_tool
# ===================================================================


class TestExecuteDynamicTool:
    """Tests for execute_dynamic_tool — runs a tool in a sandbox subprocess."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        """Auto-apply patches for every test in this class.

        Note: load_settings is imported *inside* execute_dynamic_tool
        (from aura.config), so we patch aura.config.load_settings.
        """
        with (
            patch("aura.config.load_settings") as mock_load_settings,
            patch("aura.conversation.tools.dynamic.SandboxExecutor") as mock_executor_cls,
        ):
            # Configure load_settings to return AppSettings with sandbox_mode="host"
            from aura.settings import AppSettings

            mock_load_settings.return_value = AppSettings(sandbox_mode="host")

            # Configure SandboxExecutor mock
            self.mock_executor_instance = MagicMock()
            mock_executor_cls.return_value = self.mock_executor_instance

            self.mock_load_settings = mock_load_settings
            self.mock_executor_cls = mock_executor_cls
            yield

    # -- Happy path --------------------------------------------------------

    def test_successful_execution(self, tmp_path: Path):
        """run_dynamic_tool returns valid JSON → parsed dict."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout='{"ok": true, "result": 42}',
            stderr="",
            exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def add(a: int, b: int) -> int: return a + b\n", encoding="utf-8")
        result = execute_dynamic_tool(
            file_path=fp,
            function_name="add",
            arguments={"a": 1, "b": 2},
            workspace_root=tmp_path,
        )
        assert result == {"ok": True, "result": 42}

    def test_network_disabled(self, tmp_path: Path):
        """SandboxExecutor is constructed with network_enabled=False."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True, stdout='{"ok": true}', stderr="", exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        _, kwargs = self.mock_executor_cls.call_args
        assert kwargs["network_enabled"] is False

    def test_sandbox_mode_from_settings(self, tmp_path: Path):
        """SandboxExecutor gets the mode from loaded settings."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True, stdout='{"ok": true}', stderr="", exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        _, kwargs = self.mock_executor_cls.call_args
        assert kwargs["mode"] == "host"

    # -- Error paths -------------------------------------------------------

    def test_invalid_json_stdout(self, tmp_path: Path):
        """run_dynamic_tool returns non-JSON stdout → error dict."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout="this is not json",
            stderr="some stderr message",
            exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        result = execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        assert result["ok"] is False
        assert "parse error" in result["error"].lower()
        # stderr is used in the error message when stdout isn't valid JSON
        assert "some stderr message" in result["error"]

    def test_empty_stdout(self, tmp_path: Path):
        """run_dynamic_tool returns empty stdout → JSON decode error → error dict."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout="",
            stderr="error occurred",
            exit_code=1,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        result = execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        assert result["ok"] is False
        assert "parse error" in result["error"].lower()
        assert "error occurred" in result["error"]

    def test_whitespace_only_stdout(self, tmp_path: Path):
        """run_dynamic_tool returns whitespace-only stdout → JSON decode error."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout="   \n  \n",
            stderr="whitespace error",
            exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        result = execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        assert result["ok"] is False
        assert "parse error" in result["error"].lower()

    def test_successful_with_stderr(self, tmp_path: Path):
        """run_dynamic_tool succeeds with some stderr (ignored if stdout is valid JSON)."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout='{"ok": true, "result": "hello"}',
            stderr="some warning",
            exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): return 'hello'\n", encoding="utf-8")
        result = execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        assert result == {"ok": True, "result": "hello"}

    def test_sandbox_result_ok_false(self, tmp_path: Path):
        """When SandboxResult.ok is False, empty stdout triggers JSON parse error."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=False,
            stdout="",
            stderr="tool error",
            exit_code=1,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        result = execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        assert result["ok"] is False
        assert "parse error" in result["error"].lower()
        assert "tool error" in result["error"]

    def test_timeout_value_propagated(self, tmp_path: Path):
        """Timeout value of 30 is passed through to run_dynamic_tool."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout='{"ok": true}',
            stderr="",
            exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        assert self.mock_executor_instance.run_dynamic_tool.call_args[1]["timeout"] == 30

    def test_workspace_root_propagated(self, tmp_path: Path):
        """Workspace_root is passed to SandboxExecutor constructor."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout='{"ok": true}',
            stderr="",
            exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        _, kwargs = self.mock_executor_cls.call_args
        assert kwargs["workspace_root"] == tmp_path

    def test_settings_loaded_once(self, tmp_path: Path):
        """load_settings is called exactly once during execute_dynamic_tool."""
        from aura.sandbox import SandboxResult

        self.mock_executor_instance.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout='{"ok": true}',
            stderr="",
            exit_code=0,
        )
        fp = tmp_path / "tool.py"
        fp.write_text("def f(): pass\n", encoding="utf-8")
        execute_dynamic_tool(
            file_path=fp,
            function_name="f",
            arguments={},
            workspace_root=tmp_path,
        )
        assert self.mock_load_settings.call_count == 1


# ===================================================================
# Module-level import sanity checks
# ===================================================================


class TestModuleImports:
    """Verify that module-level imports resolve correctly."""

    def test_sandbox_executor_imported(self):
        """SandboxExecutor is importable from aura.sandbox."""
        from aura.sandbox import SandboxExecutor  # noqa: F811
        assert SandboxExecutor is not None

    def test_sandbox_result_imported(self):
        """SandboxResult is importable from aura.sandbox."""
        from aura.sandbox import SandboxResult  # noqa: F811
        assert SandboxResult is not None
