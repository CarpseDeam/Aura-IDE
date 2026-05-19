from __future__ import annotations

import ast
import io
import re
import tokenize

from aura.quality.quality_model import QualityAxis, QualityIssue, QualitySeverity


def scan_comments(source: str) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    reader = io.StringIO(source).readline

    try:
        tokens = tokenize.generate_tokens(reader)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue

            text = token.string.lstrip("#").strip()
            lowered = text.lower()
            line = token.start[0]
            column = token.start[1]

            if re.match(r"^(increment|decrement|set|assign|return|get|initialize|init|create|loop through|process each|build set)\b", lowered):
                issues.append(
                    QualityIssue(
                        code="redundant_comment",
                        message="Comment restates obvious code",
                        severity=QualitySeverity.MEDIUM,
                        axis=QualityAxis.COMMENT,
                        line=line,
                        col=column,
                    )
                )

            if re.match(r"^(assuming|assumes|presumably|apparently|i think|we think|should be|might be)\b", lowered):
                issues.append(
                    QualityIssue(
                        code="assumption_comment",
                        message="Comment signals unverified behavior",
                        severity=QualitySeverity.HIGH,
                        axis=QualityAxis.COMMENT,
                        line=line,
                        col=column,
                    )
                )

            if re.match(r"^(should work|hopefully|probably|might work|try this|seems to|appears to)\b", lowered):
                issues.append(
                    QualityIssue(
                        code="hedging_comment",
                        message="Hedging comment suggests uncertainty",
                        severity=QualitySeverity.HIGH,
                        axis=QualityAxis.COMMENT,
                        line=line,
                        col=column,
                    )
                )

            if re.match(r"^(obviously|clearly|simply|just|easy|trivial|basically|of course|naturally)\b", lowered):
                issues.append(
                    QualityIssue(
                        code="overconfident_comment",
                        message="Overconfident comment adds noise",
                        severity=QualitySeverity.MEDIUM,
                        axis=QualityAxis.COMMENT,
                        line=line,
                        col=column,
                    )
                )

    except tokenize.TokenError:
        return issues

    return issues


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if not body:
        return body

    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]

    return body


def _is_ellipsis_expr(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is ...
    )


def _raises_not_implemented(stmt: ast.Raise) -> bool:
    exc = stmt.exc
    if isinstance(exc, ast.Call):
        return isinstance(exc.func, ast.Name) and exc.func.id == "NotImplementedError"
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"


def _empty_except_issue(node: ast.ExceptHandler) -> QualityIssue:
    from aura.quality.rules.misc import _issue_from_node

    if node.type is None:
        severity = QualitySeverity.CRITICAL
        message = "Bare except pass silently swallows every exception"
        suggestion = "Catch specific exception types and handle or log them."
    elif _is_import_guard_exception(node.type):
        severity = QualitySeverity.LOW
        message = "Optional dependency guard uses pass"
        suggestion = "Add a short comment explaining the fallback behavior."
    else:
        severity = QualitySeverity.MEDIUM
        message = "Typed exception handler silently discards the exception"
        suggestion = "Log, handle, or explain why silence is correct."

    return _issue_from_node(
        "empty_except",
        message,
        severity,
        QualityAxis.STRUCTURE,
        node,
        suggestion,
    )


def _is_import_guard_exception(node: ast.AST) -> bool:
    names: set[str] = set()

    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, ast.Tuple):
        names.update(item.id for item in node.elts if isinstance(item, ast.Name))

    return bool(names) and names.issubset({"ImportError", "ModuleNotFoundError"})


def _empty_container_repr(value: ast.expr | None) -> str | None:
    if isinstance(value, ast.List) and not value.elts:
        return "[]"
    if isinstance(value, ast.Dict) and not value.keys:
        return "{}"
    if isinstance(value, ast.Tuple) and not value.elts:
        return "()"
    if isinstance(value, ast.Set) and not value.elts:
        return "set()"
    return None
