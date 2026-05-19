from __future__ import annotations

import ast

from aura.quality.quality_model import QualityAxis, QualityIssue, QualitySeverity
from aura.quality.rules.comments import (
    _empty_container_repr,
    _empty_except_issue,
    _is_ellipsis_expr,
    _raises_not_implemented,
    _strip_docstring,
)
from aura.quality.rules.misc import _has_abstract_decorator, _issue_from_node

DUNDER_CONSTANT_OK = {
    "__len__",
    "__bool__",
    "__hash__",
    "__sizeof__",
    "__index__",
    "__int__",
    "__float__",
    "__complex__",
    "__str__",
    "__repr__",
    "__bytes__",
    "__format__",
}


def check_placeholder_node(node: ast.AST) -> list[QualityIssue]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _has_abstract_decorator(node):
            return []

        body = _strip_docstring(node.body)

        if len(body) == 1 and isinstance(body[0], ast.Pass):
            return [
                _issue_from_node(
                    "pass_placeholder",
                    "Function body is only pass",
                    QualitySeverity.HIGH,
                    QualityAxis.PLACEHOLDER,
                    node,
                    "Implement the function or remove it.",
                )
            ]

        if len(body) == 1 and _is_ellipsis_expr(body[0]):
            return [
                _issue_from_node(
                    "ellipsis_placeholder",
                    "Function body is only ellipsis",
                    QualitySeverity.HIGH,
                    QualityAxis.PLACEHOLDER,
                    node,
                    "Implement the function or remove it.",
                )
            ]

        if len(body) == 1 and isinstance(body[0], ast.Raise):
            if _raises_not_implemented(body[0]):
                return [
                    _issue_from_node(
                        "not_implemented",
                        "Function only raises NotImplementedError",
                        QualitySeverity.HIGH,
                        QualityAxis.PLACEHOLDER,
                        node,
                        "Implement the function or make it an intentional abstract method.",
                    )
                ]

        if len(body) == 1 and isinstance(body[0], ast.Return):
            return check_placeholder_return(node, body[0])

    if isinstance(node, ast.ExceptHandler):
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            return [_empty_except_issue(node)]

    return []


def check_placeholder_return(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    ret: ast.Return,
) -> list[QualityIssue]:
    if node.name.startswith("__") and node.name.endswith("__"):
        return []

    if ret.value is None:
        return [
            _issue_from_node(
                "return_none_placeholder",
                "Function only returns None",
                QualitySeverity.MEDIUM,
                QualityAxis.PLACEHOLDER,
                node,
                "Implement the function or clarify intent.",
            )
        ]

    if isinstance(ret.value, ast.Constant):
        if ret.value.value is None:
            return [
                _issue_from_node(
                    "return_none_placeholder",
                    "Function only returns None",
                    QualitySeverity.MEDIUM,
                    QualityAxis.PLACEHOLDER,
                    node,
                    "Implement the function or clarify intent.",
                )
            ]

        if node.name not in DUNDER_CONSTANT_OK:
            return [
                _issue_from_node(
                    "return_constant_stub",
                    f"Function only returns constant {ret.value.value!r}",
                    QualitySeverity.HIGH,
                    QualityAxis.PLACEHOLDER,
                    node,
                    "Implement meaningful logic or remove the stub.",
                )
            ]

    if _empty_container_repr(ret.value) is not None:
        return [
            _issue_from_node(
                "return_constant_stub",
                f"Function only returns empty {_empty_container_repr(ret.value)}",
                QualitySeverity.HIGH,
                QualityAxis.PLACEHOLDER,
                node,
                "Implement meaningful logic or remove the stub.",
            )
        ]

    return []
