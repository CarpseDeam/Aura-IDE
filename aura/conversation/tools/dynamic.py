"""Self-extending tools: AST-based schema parsing and subprocess execution.

Dynamic tools are user-created Python scripts in ``.aura/tools/``. This module
parses their signatures into OpenAI tool definitions and executes them in
isolated subprocesses so bugs cannot crash the IDE.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from aura.sandbox import SandboxExecutor, SandboxResult


def _get_base_name(node: ast.expr) -> str:
    """Extract a human-readable base name from an annotation expression.

    ast.Name("list")       → "list"
    ast.Attribute(..., "List") → "List"
    anything else           → ""
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _annotation_to_json_type(ann_node: ast.expr | None) -> str:
    """Convert an AST annotation node to a JSON Schema type string.

    Handles simple types (str, int, float, bool, None), PEP 604 unions
    (str | None), typing generics (Optional, List, Dict), and falls back
    to "string" for anything unrecognised.
    """
    if ann_node is None:
        return "string"

    # --- PEP 604 union: X | Y  → extract the first non-None side ---
    if isinstance(ann_node, ast.BinOp):
        # We only handle the | operator (BitOr)
        if isinstance(ann_node.op, ast.BitOr):
            # Try each side; prefer the non-None one
            for side in (ann_node.left, ann_node.right):
                t = _annotation_to_json_type(side)
                if t != "null":
                    return t
            return "null"
        return "string"

    # --- ast.Constant: e.g. None literal ---
    if isinstance(ann_node, ast.Constant):
        if ann_node.value is None:
            return "null"
        return "string"

    # --- ast.Name: e.g. str, int, float, bool, None, Any ---
    if isinstance(ann_node, ast.Name):
        mapping = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "None": "null",
        }
        return mapping.get(ann_node.id, "string")

    # --- ast.Subscript: e.g. list[str], Optional[str], typing.List[int] ---
    if isinstance(ann_node, ast.Subscript):
        # Extract the base name, handling both Name("list") and
        # Attribute(Name("typing"), "List")
        base_id = _get_base_name(ann_node.value)
        if base_id in ("list", "List", "Sequence", "MutableSequence"):
            return "array"
        if base_id in ("dict", "Dict", "Mapping", "MutableMapping"):
            return "object"
        if base_id in ("Optional",):
            # typing.Optional[X] — extract X (the slice)
            return _annotation_to_json_type(ann_node.slice)
        # Anything else (including Union, Tuple, Set, etc.) → string
        return "string"

    # --- Conservative fallback ---
    return "string"


def _parse_docstring_args(docstring: str | None) -> dict[str, str]:
    """Extract parameter descriptions from a Google-style docstring Args block.

    Parses lines of the form ``param_name: description text`` within an
    ``Args:`` section. Returns a dict mapping parameter names to their
    descriptions.
    """
    if not docstring:
        return {}

    descriptions: dict[str, str] = {}
    in_args = False
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if in_args:
            # Stop at the next section (a non-indented line that isn't a
            # continuation of the Args block).
            if stripped == "":
                continue
            if not line.startswith(" ") and not line.startswith("\t"):
                # A non-indented, non-empty line ends the Args block.
                in_args = False
                continue
            # Parse "param_name: description"
            if ":" in stripped:
                param_name, _, desc = stripped.partition(":")
                param_name = param_name.strip()
                desc = desc.strip()
                if param_name:
                    descriptions[param_name] = desc

    return descriptions


def parse_tool_schema(file_path: Path) -> dict[str, Any]:
    """Parse a Python source file and return an OpenAI tool definition dict.

    Uses the ``ast`` module to find the first top-level function definition,
    extract its name, docstring, and typed parameters, and build a JSON Schema
    ``parameters`` block.

    Args:
        file_path: Path to a ``.py`` file containing a top-level function.

    Returns:
        An OpenAI tool definition dict with ``type``, ``function`` (name,
        description, parameters).

    Raises:
        ValueError: If the file has no top-level function or cannot be parsed.
    """
    # Parse the source
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        raise ValueError(f"Syntax error in {file_path}: {exc}") from exc

    # Find the first top-level function definition
    func_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_node = node
            break

    if func_node is None:
        raise ValueError(f"No top-level function found in {file_path}")

    # --- Name ---
    func_name = func_node.name

    # --- Docstring / description ---
    docstring = ast.get_docstring(func_node)
    if docstring:
        # Use the first paragraph (up to the first blank line) as the
        # description, or the full docstring if it's a single paragraph.
        paragraphs = docstring.strip().split("\n\n")
        description = paragraphs[0].replace("\n", " ").strip()
    else:
        description = f"Dynamic tool: {func_name}"

    # --- Parameters ---
    arg_descriptions = _parse_docstring_args(docstring)

    # Build the set of argument indices that have defaults.
    # Defaults are aligned to the *last* N positional args.
    num_defaults = len(func_node.args.defaults)
    num_args = len(func_node.args.args)
    # The first (num_args - num_defaults) args have no default → required.
    default_offset = num_args - num_defaults

    properties: dict[str, Any] = {}
    required: list[str] = []

    for i, arg in enumerate(func_node.args.args):
        arg_name = arg.arg
        json_type = _annotation_to_json_type(arg.annotation)
        arg_desc = arg_descriptions.get(arg_name, "")

        properties[arg_name] = {
            "type": json_type,
            "description": arg_desc,
        }

        if i < default_offset:
            required.append(arg_name)

    # Handle *args (vararg)
    if func_node.args.vararg:
        vararg_name = func_node.args.vararg.arg
        vararg_desc = arg_descriptions.get(vararg_name, "")
        properties[vararg_name] = {
            "type": "array",
            "description": vararg_desc,
        }
        # vararg is never required

    # Handle **kwargs (kwarg)
    if func_node.args.kwarg:
        kwarg_name = func_node.args.kwarg.arg
        kwarg_desc = arg_descriptions.get(kwarg_name, "")
        properties[kwarg_name] = {
            "type": "object",
            "description": kwarg_desc,
        }
        # kwarg is never required

    return {
        "type": "function",
        "function": {
            "name": func_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def execute_dynamic_tool(
    file_path: Path,
    function_name: str,
    arguments: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    """Execute a dynamic tool function in an isolated subprocess.

    The function at ``file_path`` is loaded via ``importlib`` in a fresh
    Python process.  Arguments are passed as JSON on stdin, and the result
    (or error) is returned as JSON on stdout.

    When sandbox_mode is 'docker', runs inside a lightweight Docker container
    with resource limits and capability dropping for true OS-level isolation.

    Args:
        file_path: Path to the ``.py`` file containing the function.
        function_name: Name of the function to call.
        arguments: Keyword arguments to pass to the function.
        workspace_root: Working directory for the subprocess.

    Returns:
        A dict with ``ok`` (bool) and either ``result`` or ``error`` (str).
    """
    from aura.config import load_settings

    settings = load_settings()
    sandbox = SandboxExecutor(
        mode=settings.sandbox_mode,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        network_enabled=False,  # Dynamic tools should not need network
    )

    result: SandboxResult = sandbox.run_dynamic_tool(
        file_path=file_path,
        function_name=function_name,
        arguments=arguments,
        timeout=30,
    )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"Dynamic tool output parse error: {stderr or stdout}",
        }

    return parsed
