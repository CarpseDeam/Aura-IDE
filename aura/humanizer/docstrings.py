from __future__ import annotations

import ast
import re

_CONTRACT_MARKERS = re.compile(
    r"(?:^|\b)(Args|Returns|Raises|Yields|Example|Note|Warning|Deprecated)\s*:"
)


def remove_internal_docstrings(code: str) -> tuple[str, int]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return (code, 0)

    lines = code.splitlines(keepends=True)
    removals: list[tuple[int, int]] = []  # (start_line_1idx, end_line_1idx) inclusive

    _collect_docstring_removals(tree, lines, removals)

    if not removals:
        return (code, 0)

    # Sort in reverse order so line numbers stay valid
    removals.sort(key=lambda r: r[0], reverse=True)

    for start, end in removals:
        # Remove trailing blank line after the docstring if present
        if end + 1 <= len(lines) and _is_blank_line(lines[end]):
            end += 1
        del lines[start - 1 : end]

    return ("".join(lines), len(removals))


def _collect_docstring_removals(
    node: ast.AST,
    lines: list[str],
    removals: list[tuple[int, int]],
) -> None:
    """Walk the AST and collect docstring ranges that should be removed."""
    for child in ast.iter_child_nodes(node):
        _collect_docstring_removals(child, lines, removals)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        _handle_docstring_removal(node, lines, removals)


def _handle_docstring_removal(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    lines: list[str],
    removals: list[tuple[int, int]],
) -> None:
    docstr = _get_docstring_expr(node)
    if docstr is None:
        return

    doc_text = docstr.value.value  # type: ignore[union-attr]

    if isinstance(node, ast.ClassDef):
        # Only remove private class docstrings
        if node.name.startswith("_") and not _is_magic_name(node.name):
            removals.append((docstr.lineno, docstr.end_lineno))
        return

    # FunctionDef / AsyncFunctionDef
    name = node.name

    # Keep docstrings from public functions and magic/dunder methods
    if not name.startswith("_"):
        return
    if _is_magic_name(name):
        return

    # Check if docstring has real contract information
    if _has_contract_info(doc_text):
        return

    # Check if docstring is long (>80 chars)
    if len(doc_text) > 80:
        return

    # Check if function body (excluding docstring) is > 5 lines
    if not _is_short_body(node):
        return

    # If we get here, remove the docstring
    removals.append((docstr.lineno, docstr.end_lineno))


def _get_docstring_expr(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module,
) -> ast.Expr | None:
    """Return the docstring Expr node if the first body statement is a docstring."""
    if not node.body:
        return None
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first
    return None


def _has_contract_info(doc_text: str) -> bool:
    return bool(_CONTRACT_MARKERS.search(doc_text))


def _is_short_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if the function body (excluding docstring) is ≤ 5 lines."""
    body_stmts = node.body
    # Skip docstring if present
    if body_stmts and isinstance(body_stmts[0], ast.Expr) and isinstance(body_stmts[0].value, ast.Constant):
        body_stmts = body_stmts[1:]

    if not body_stmts:
        return True

    first_line = body_stmts[0].lineno
    last_line = body_stmts[-1].end_lineno
    return (last_line - first_line + 1) <= 5


def _is_magic_name(name: str) -> bool:
    """Return True if the name is a dunder/magic method like __init__."""
    return name.startswith("__") and name.endswith("__")


def _is_blank_line(line: str) -> bool:
    return line.strip() == ""
