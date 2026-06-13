"""AST-based structured editing: edit_symbol tool for Python files.

Uses Python's built-in ``ast`` module to locate functions, classes, and
methods by name, completely bypassing string matching for structured edits.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from aura.ast_utils import parse_python_ast
from aura.conversation.tools.fs_write import replace_line_range
from aura.paths import safe_relative_to


def _collect_available_symbols(tree: ast.AST) -> dict[str, list[str]]:
    available: dict[str, list[str]] = {"functions": [], "classes": [], "methods": []}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            available["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            available["classes"].append(node.name)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    available["methods"].append(f"{node.name}.{child.name}")
    return available


def find_symbol_range(
    source: str,
    symbol_type: str,
    symbol_name: str,
    class_name: str | None = None,
    filename: str = "<unknown>",
) -> tuple[int, int, dict[str, Any]]:
    """Locate a Python symbol in *source* and return its 0-indexed line range.

    Args:
        source: The full file content as a string.
        symbol_type: ``"function"``, ``"class"``, or ``"method"``.
            If ``"function"`` and *class_name* is provided, it is treated as
            ``"method"`` automatically.
        symbol_name: The name of the symbol to locate.
        class_name: Required when *symbol_type* is ``"method"`` — the name
            of the class containing the method.

    Returns:
        ``(start_line, end_line, info_dict)`` where *start_line* and
        *end_line* are 0-indexed (exclusive end), and *info_dict* contains
        metadata such as warnings. If the symbol is not found, the first two
        elements are ``(-1, -1)`` and *info_dict* contains the key
        ``"available_symbols"`` with a list of top-level names.

    Raises:
        SyntaxError: If *source* cannot be parsed.
    """
    tree = parse_python_ast(source, filename=filename)
    warning = None

    # If symbol_type is "function" but class_name is provided, treat as method.
    effective_type = symbol_type
    if symbol_type == "function" and class_name:
        effective_type = "method"

    available = _collect_available_symbols(tree)

    if effective_type == "method":
        if not class_name:
            return (-1, -1, {
                "error": "class_name is required when symbol_type is 'method'",
                "available_symbols": available,
            })
        # Find the class first.
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                # Now find the method inside the class body.
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == symbol_name:
                        start = child.lineno - 1
                        if hasattr(child, "decorator_list") and child.decorator_list:
                            start = child.decorator_list[0].lineno - 1
                        end = child.end_lineno  # end_lineno is already 1-indexed inclusive
                        return (start, end, {"warning": warning})
                # Method not found in class.
                return (-1, -1, {
                    "error": f"Method '{symbol_name}' not found in class '{class_name}'",
                    "available_symbols": available,
                })
        # Class not found.
        return (-1, -1, {
            "error": f"Class '{class_name}' not found",
            "available_symbols": available,
        })

    # Top-level function or class.
    found = None
    for node in ast.iter_child_nodes(tree):
        if effective_type == "function" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == symbol_name:
                if found is not None:
                    warning = f"Multiple symbols named '{symbol_name}' found; using first occurrence"
                if found is None:
                    found = node
        elif effective_type == "class" and isinstance(node, ast.ClassDef):
            if node.name == symbol_name:
                if found is not None:
                    warning = f"Multiple symbols named '{symbol_name}' found; using first occurrence"
                if found is None:
                    found = node

    if found is not None:
        start = found.lineno - 1
        if hasattr(found, "decorator_list") and found.decorator_list:
            start = found.decorator_list[0].lineno - 1
        end = found.end_lineno  # end_lineno is 1-indexed inclusive
        return (start, end, {"warning": warning})

    return (-1, -1, {
        "error": (
            f"Symbol '{symbol_name}' of type '{symbol_type}' not found. "
            f"Available top-level functions: {available['functions']}. "
            f"Available classes: {available['classes']}."
        ),
        "available_symbols": available,
    })


def propose_edit_symbol(
    workspace_root: Path,
    target: Path,
    symbol_type: str,
    symbol_name: str,
    new_definition: str,
    class_name: str | None = None,
) -> dict[str, Any]:
    """Replace a named Python symbol (function, class, or method) using AST.

    This completely bypasses string matching — it uses the ``ast`` module to
    locate the symbol's exact line range and replaces it with *new_definition*.

    Args:
        workspace_root: Resolved root of the workspace jail.
        target: The ``.py`` file to edit.
        symbol_type: ``\"function\"``, ``\"class\"``, or ``\"method\"``.
        symbol_name: Name of the symbol to replace.
        new_definition: The complete new definition (decorators, signature,
            docstring, body). This replaces the entire existing definition.
        class_name: Required when *symbol_type* is ``\"method\"``.

    Returns:
        A dict with the same shape as :func:`propose_edit`:

        - ``ok``: bool
        - ``rel_path``: str (POSIX-relative path)
        - ``old_content``: str (original file content)
        - ``new_content``: str (file content after replacement) or ``\"\"`` on error
        - ``is_new_file``: ``False``
        - ``match_tier``: ``\"symbol\"`` on success
    """
    # --- Validation -----------------------------------------------------------
    if not target.exists():
        rel = _rel_path(workspace_root, target)
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "old_content": "",
            "new_content": "",
            "is_new_file": False,
            "error": f"file not found: {rel}",
            "failure_class": "path_error",
        }
    if not target.is_file():
        rel = _rel_path(workspace_root, target)
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "old_content": "",
            "new_content": "",
            "is_new_file": False,
            "error": f"not a regular file: {rel}",
            "failure_class": "path_error",
        }
    if target.suffix != ".py":
        rel = _rel_path(workspace_root, target)
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "old_content": "",
            "new_content": "",
            "is_new_file": False,
            "error": "edit_symbol only supports Python (.py) files. Use patch_file for other languages.",
            "failure_class": "path_error",
        }

    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        rel = _rel_path(workspace_root, target)
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "old_content": "",
            "new_content": "",
            "is_new_file": False,
            "error": "file is not valid UTF-8 text",
            "failure_class": "internal_error",
        }

    rel = _rel_path(workspace_root, target)

    # --- Parse and locate symbol ----------------------------------------------
    try:
        start_line, end_line, info = find_symbol_range(
            original, symbol_type, symbol_name, class_name, filename=str(target)
        )
    except SyntaxError as exc:
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "old_content": original,
            "new_content": "",
            "is_new_file": False,
            "error": f"Syntax error in file: {exc}",
            "failure_class": "syntax_invalid",
            "available_symbols": {},
            "suggested_tool": "write_file",
            "suggested_next_tool": "write_file",
            "suggested_next_action": "The file is not parseable. Use write_file to repair the Python syntax.",
        }

    if start_line == -1:
        available = info.get("available_symbols", {})
        has_symbols = any(available.values())
        error_extra = info.get("error", f"Symbol '{symbol_name}' not found")
        suggested_tool = "patch_file" if has_symbols else "write_file"
        suggested_next_action = (
            "Use read_file_outline or read_file to inspect available symbols, then use patch_file with current exact text."
            if has_symbols
            else "No parseable symbols were available. Use write_file for a full-file repair or replacement."
        )
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "old_content": original,
            "new_content": "",
            "is_new_file": False,
            "error": error_extra,
            "failure_class": "edit_mechanics_symbol_not_found",
            "symbol_type": symbol_type,
            "symbol_name": symbol_name,
            "class_name": class_name,
            "suggested_tool": suggested_tool,
            "suggested_next_tool": suggested_tool,
            "suggested_next_action": suggested_next_action,
            "available_symbols": available,
            "suggested_fallback": (
                "read_file_outline/read_file then patch_file"
                if has_symbols
                else "write_file"
            ),
        }

    # --- Compute replacement --------------------------------------------------
    lines_with_nl = original.splitlines(keepends=True)

    orig_block = "".join(lines_with_nl[start_line:end_line])

    # Auto-indent new_definition to match the original symbol's indentation.
    # This handles methods inside classes where the replacement body is often
    # given without class-level indentation.
    first_orig_line = orig_block.split("\n", 1)[0]
    orig_indent = first_orig_line[: len(first_orig_line) - len(first_orig_line.lstrip())]
    if orig_indent:
        first_new_line = new_definition.split("\n", 1)[0]
        if not first_new_line.startswith(orig_indent):
            indented = "\n".join(
                (orig_indent + line) if line else ""
                for line in new_definition.split("\n")
            )
            new_definition = indented

    # Normalise new_definition trailing newline to match original block's style.
    # If the original block ended with a newline and new_definition doesn't, add one.
    # If the original file didn't end with a newline and this was the last block,
    # preserve that.
    orig_ends_with_nl = orig_block.endswith("\n")

    if orig_ends_with_nl and not new_definition.endswith("\n"):
        new_definition = new_definition + "\n"
    elif not orig_ends_with_nl and new_definition.endswith("\n"):
        new_definition = new_definition.rstrip("\n")

    new_content = replace_line_range(original, lines_with_nl, start_line, end_line, new_definition)

    # --- Validate replacement produces valid Python --------------------------
    try:
        parse_python_ast(new_content, filename=str(target))
    except SyntaxError:
        import sys
        syntax_error = sys.exc_info()[1]
        return {
            "ok": False,
            "path": rel,
            "rel_path": rel,
            "old_content": original,
            "new_content": "",
            "is_new_file": False,
            "error": f"Proposed replacement makes the file invalid: {syntax_error}",
            "failure_class": "syntax_invalid",
            "symbol_type": symbol_type,
            "symbol_name": symbol_name,
            "class_name": class_name,
            "suggested_tool": "patch_file",
            "suggested_next_tool": "patch_file",
            "suggested_next_action": "Repair the replacement syntax before any unrelated tool call.",
        }

    result: dict[str, Any] = {
        "ok": True,
        "path": rel,
        "rel_path": rel,
        "old_content": original,
        "new_content": new_content,
        "is_new_file": False,
        "match_tier": "symbol",
    }
    if info.get("warning"):
        result["warning"] = info["warning"]

    return result


def _rel_path(workspace_root: Path, target: Path) -> str:
    """Return a POSIX-relative path string for a target file."""
    return safe_relative_to(target, workspace_root).as_posix()
