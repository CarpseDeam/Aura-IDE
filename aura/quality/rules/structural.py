from __future__ import annotations

import ast

from aura.quality.quality_model import QualityAxis, QualityIssue, QualitySeverity
from aura.quality.rules.misc import (
    _has_significant_base,
    _has_special_class_decorator,
    _is_interface_class,
    _issue,
    _issue_from_node,
)


def check_structural_node(node: ast.AST, lines: list[str]) -> list[QualityIssue]:
    if isinstance(node, ast.ExceptHandler) and node.type is None:
        return [
            _issue(
                "bare_except",
                "Bare except catches everything including SystemExit and KeyboardInterrupt",
                QualitySeverity.CRITICAL,
                QualityAxis.STRUCTURE,
                node,
                lines,
                "Catch specific exception types.",
            )
        ]

    if isinstance(node, ast.FunctionDef):
        for default in node.args.defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                return [
                    _issue(
                        "mutable_default_arg",
                        "Mutable default argument can leak shared state between calls",
                        QualitySeverity.CRITICAL,
                        QualityAxis.STRUCTURE,
                        node,
                        lines,
                        "Use None as the default and initialize inside the function.",
                    )
                ]

    if isinstance(node, ast.ImportFrom):
        if any(alias.name == "*" for alias in node.names):
            return [
                _issue(
                    "star_import",
                    "Star import pollutes namespace and hides dependencies",
                    QualitySeverity.HIGH,
                    QualityAxis.STRUCTURE,
                    node,
                    lines,
                    "Import specific names instead.",
                )
            ]

    if isinstance(node, ast.Global):
        return [
            _issue(
                "global_statement",
                "Global statement makes code harder to test and reason about",
                QualitySeverity.HIGH,
                QualityAxis.STRUCTURE,
                node,
                lines,
                "Pass state explicitly or keep it on an object.",
            )
        ]

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"exec", "eval"}:
            return [
                _issue(
                    "exec_eval_usage",
                    "exec/eval is a security and maintenance risk",
                    QualitySeverity.CRITICAL,
                    QualityAxis.STRUCTURE,
                    node,
                    lines,
                    "Refactor to avoid dynamic code execution.",
                )
            ]

    return []


def check_single_method_class(node: ast.AST) -> list[QualityIssue]:
    if not isinstance(node, ast.ClassDef):
        return []

    if _is_interface_class(node) or _has_special_class_decorator(node) or _has_significant_base(node):
        return []

    special = {"__init__", "__new__", "__del__", "__repr__", "__str__"}
    public_methods = [
        item
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name not in special
        and not item.name.startswith("_")
    ]
    special_methods = [
        item
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name in special
    ]

    if len(public_methods) == 1 and len(special_methods) <= 1:
        return [
            _issue_from_node(
                "single_method_class",
                f"Class {node.name!r} has only one public method {public_methods[0].name!r}",
                QualitySeverity.HIGH,
                QualityAxis.STRUCTURE,
                node,
                "Use a function unless the class has real state or a clear domain role.",
            )
        ]

    return []
