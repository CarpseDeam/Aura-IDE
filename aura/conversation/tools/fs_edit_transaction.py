"""Deterministic high-level edit transactions for existing files."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from aura.ast_utils import parse_python_ast
from aura.conversation.tools.fs_write import _failure_payload, _rel_path, replace_line_range


_SYMBOL_NODE_TYPES = {
    "function": (ast.FunctionDef, ast.AsyncFunctionDef),
    "method": (ast.FunctionDef, ast.AsyncFunctionDef),
    "class": (ast.ClassDef,),
}


def _first_string(op: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = op.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _operation_symbol_name(kind: str, op: dict[str, Any]) -> str | None:
    if kind == "replace_function":
        return _first_string(op, "symbol_name", "function_name", "name")
    if kind == "replace_method":
        return _first_string(op, "symbol_name", "method_name", "name")
    if kind == "replace_class":
        return _first_string(op, "symbol_name", "class_name", "name")
    if kind == "insert_after_symbol":
        symbol_type = str(op.get("symbol_type") or "")
        if symbol_type == "function":
            return _first_string(op, "symbol_name", "function_name", "name")
        if symbol_type == "method":
            return _first_string(op, "symbol_name", "method_name", "name")
        if symbol_type == "class":
            return _first_string(op, "symbol_name", "class_name", "name")
        return _first_string(op, "symbol_name", "function_name", "method_name", "class_name", "name")
    return _first_string(op, "symbol_name", "name")


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def _normalize_newlines(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def _available_symbols(tree: ast.AST) -> dict[str, list[str]]:
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


def _node_range(node: ast.AST) -> tuple[int, int]:
    start = node.lineno - 1
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        start = decorators[0].lineno - 1
    return start, int(node.end_lineno)


def _find_symbol(
    source: str,
    *,
    symbol_type: str,
    symbol_name: str,
    class_name: str | None = None,
    filename: str,
) -> tuple[int, int, dict[str, Any]]:
    tree = parse_python_ast(source, filename=filename)
    available = _available_symbols(tree)
    effective_type = "method" if class_name and symbol_type == "function" else symbol_type
    if effective_type not in _SYMBOL_NODE_TYPES:
        return -1, -1, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": f"unsupported symbol_type: {symbol_type}",
            "available_symbols": available,
        }

    if effective_type == "method":
        if not class_name:
            return -1, -1, {
                "failure_class": "edit_transaction_invalid_operation",
                "error": "class_name is required for method operations",
                "available_symbols": available,
            }
        classes = [
            node for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        if not classes:
            return -1, -1, {
                "failure_class": "edit_transaction_symbol_not_found",
                "error": f"Class '{class_name}' not found",
                "available_symbols": available,
            }
        if len(classes) > 1:
            return -1, -1, {
                "failure_class": "edit_transaction_ambiguous_symbol",
                "error": f"Class '{class_name}' is ambiguous",
                "available_symbols": available,
            }
        matches = [
            child for child in ast.iter_child_nodes(classes[0])
            if isinstance(child, _SYMBOL_NODE_TYPES["method"]) and child.name == symbol_name
        ]
    else:
        matches = [
            node for node in ast.iter_child_nodes(tree)
            if isinstance(node, _SYMBOL_NODE_TYPES[effective_type]) and node.name == symbol_name
        ]

    if not matches:
        return -1, -1, {
            "failure_class": "edit_transaction_symbol_not_found",
            "error": f"{effective_type.title()} '{symbol_name}' not found",
            "available_symbols": available,
        }
    if len(matches) > 1:
        return -1, -1, {
            "failure_class": "edit_transaction_ambiguous_symbol",
            "error": f"{effective_type.title()} '{symbol_name}' is ambiguous",
            "available_symbols": available,
        }
    start, end = _node_range(matches[0])
    return start, end, {"available_symbols": available}


def _indent_like_existing(original_block: str, replacement: str) -> str:
    first_orig_line = original_block.splitlines()[0] if original_block.splitlines() else ""
    orig_indent = first_orig_line[: len(first_orig_line) - len(first_orig_line.lstrip())]
    if not orig_indent:
        return replacement
    first_new_line = replacement.splitlines()[0] if replacement.splitlines() else ""
    if first_new_line.startswith(orig_indent):
        return replacement
    return "\n".join((orig_indent + line) if line else "" for line in replacement.split("\n"))


def _replace_symbol(
    proposed: str,
    *,
    target: Path,
    symbol_type: str,
    symbol_name: str,
    new_definition: str,
    class_name: str | None,
    newline: str,
) -> tuple[bool, str, dict[str, Any]]:
    try:
        start, end, info = _find_symbol(
            proposed,
            symbol_type=symbol_type,
            symbol_name=symbol_name,
            class_name=class_name,
            filename=str(target),
        )
    except SyntaxError as exc:
        return False, proposed, {
            "failure_class": "edit_transaction_not_applicable",
            "error": f"Current Python is not parseable: {exc}",
        }
    if start < 0:
        return False, proposed, info

    lines = proposed.splitlines(keepends=True)
    old_block = "".join(lines[start:end])
    replacement = _normalize_newlines(new_definition, "\n")
    replacement = _indent_like_existing(old_block, replacement)
    replacement = _normalize_newlines(replacement, newline)
    if old_block.endswith(("\n", "\r")) and not replacement.endswith(("\n", "\r")):
        replacement += newline
    if not old_block.endswith(("\n", "\r")) and replacement.endswith(("\n", "\r")):
        replacement = replacement.rstrip("\r\n")
    return True, replace_line_range(proposed, lines, start, end, replacement), {}


def _insert_after_symbol(
    proposed: str,
    *,
    target: Path,
    symbol_type: str,
    symbol_name: str,
    class_name: str | None,
    content: str,
    newline: str,
) -> tuple[bool, str, dict[str, Any]]:
    try:
        start, end, info = _find_symbol(
            proposed,
            symbol_type=symbol_type,
            symbol_name=symbol_name,
            class_name=class_name,
            filename=str(target),
        )
    except SyntaxError as exc:
        return False, proposed, {
            "failure_class": "edit_transaction_not_applicable",
            "error": f"Current Python is not parseable: {exc}",
        }
    if start < 0:
        return False, proposed, info

    lines = proposed.splitlines(keepends=True)
    insertion = _normalize_newlines(content, newline)
    if end > 0 and not lines[end - 1].endswith(("\n", "\r")):
        insertion = newline + insertion
    if insertion and not insertion.endswith(("\n", "\r")):
        insertion += newline
    return True, replace_line_range(proposed, lines, end, end, insertion), {}


def _replace_text_once(
    proposed: str,
    *,
    old: str,
    new: str,
    newline: str,
) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(old, str) or not isinstance(new, str) or old == "":
        return False, proposed, {
            "failure_class": "edit_transaction_invalid_operation",
            "error": "replace_text_once requires non-empty string old and string new",
        }
    old = _normalize_newlines(old, newline)
    new = _normalize_newlines(new, newline)
    count = proposed.count(old)
    if count == 0:
        return False, proposed, {
            "failure_class": "edit_transaction_not_applicable",
            "error": "replace_text_once old text was not found",
        }
    if count > 1:
        return False, proposed, {
            "failure_class": "edit_transaction_ambiguous_symbol",
            "error": "replace_text_once old text is ambiguous",
            "occurrence_count": count,
        }
    return True, proposed.replace(old, new, 1), {}


def propose_edit_transaction(
    workspace_root: Path,
    target: Path,
    operations: list[dict[str, Any]],
    expected_file_hash: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Propose an atomic structured edit transaction for one existing file."""
    rel = _rel_path(workspace_root, target)
    if not target.exists():
        return _failure_payload(workspace_root, target, f"file not found: {rel}", "path_error")
    if not target.is_file():
        return _failure_payload(workspace_root, target, f"not a regular file: {rel}", "path_error")
    try:
        original = target.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return _failure_payload(workspace_root, target, "file is not valid UTF-8 text", "internal_error")
    except OSError:
        return _failure_payload(workspace_root, target, "failed to read file", "internal_error")

    if expected_file_hash is not None:
        current_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        if current_hash != expected_file_hash:
            return _failure_payload(
                workspace_root,
                target,
                "File content did not match expected_file_hash.",
                "edit_transaction_hash_mismatch",
            )
    if not operations:
        return _failure_payload(
            workspace_root,
            target,
            "operations must contain at least one operation",
            "edit_transaction_invalid_operation",
            old_content=original,
            new_content="",
            is_new_file=False,
        )

    newline = _dominant_newline(original)
    proposed = original
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            return _failure_payload(
                workspace_root,
                target,
                "each operation must be an object",
                "edit_transaction_invalid_operation",
                operation_index=index,
            )
        kind = op.get("op") or op.get("type")
        if kind in {"replace_function", "replace_method", "replace_class"}:
            symbol_type = {
                "replace_function": "function",
                "replace_method": "method",
                "replace_class": "class",
            }[str(kind)]
            symbol_name = _operation_symbol_name(str(kind), op)
            new_definition = op.get("new_definition")
            class_name = op.get("class_name")
            if not isinstance(symbol_name, str) or not isinstance(new_definition, str):
                return _failure_payload(
                    workspace_root,
                    target,
                    f"{kind} requires symbol_name and new_definition strings",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                )
            ok, proposed, failure = _replace_symbol(
                proposed,
                target=target,
                symbol_type=symbol_type,
                symbol_name=symbol_name,
                new_definition=new_definition,
                class_name=str(class_name) if class_name is not None else None,
                newline=newline,
            )
        elif kind == "insert_after_symbol":
            symbol_type = op.get("symbol_type")
            symbol_name = _operation_symbol_name(str(kind), op)
            content = op.get("content")
            class_name = op.get("class_name")
            if not isinstance(symbol_type, str) or not isinstance(symbol_name, str) or not isinstance(content, str):
                return _failure_payload(
                    workspace_root,
                    target,
                    "insert_after_symbol requires symbol_type, symbol_name, and content strings",
                    "edit_transaction_invalid_operation",
                    operation_index=index,
                )
            ok, proposed, failure = _insert_after_symbol(
                proposed,
                target=target,
                symbol_type=symbol_type,
                symbol_name=symbol_name,
                class_name=str(class_name) if class_name is not None else None,
                content=content,
                newline=newline,
            )
        elif kind == "replace_text_once":
            ok, proposed, failure = _replace_text_once(
                proposed,
                old=op.get("old"),
                new=op.get("new"),
                newline=newline,
            )
        else:
            return _failure_payload(
                workspace_root,
                target,
                f"unsupported edit transaction operation: {kind}",
                "edit_transaction_invalid_operation",
                operation_index=index,
            )
        if not ok:
            failure.setdefault("failure_class", "edit_transaction_not_applicable")
            failure.setdefault("error", "edit transaction operation could not be applied")
            return _failure_payload(
                workspace_root,
                target,
                str(failure.get("error")),
                str(failure.get("failure_class")),
                operation_index=index,
                old_content=original,
                new_content="",
                is_new_file=False,
                **{k: v for k, v in failure.items() if k not in {"error", "failure_class"}},
            )

    if target.suffix == ".py":
        try:
            parse_python_ast(proposed, filename=str(target))
        except SyntaxError as exc:
            return _failure_payload(
                workspace_root,
                target,
                f"transaction produces invalid Python: {exc}",
                "edit_transaction_invalid_syntax",
                old_content=original,
                new_content="",
                is_new_file=False,
            )

    return {
        "ok": True,
        "path": rel,
        "rel_path": rel,
        "old_content": original,
        "new_content": proposed,
        "is_new_file": False,
        "operation_count": len(operations),
        "description": description or "",
    }
