from __future__ import annotations

import ast

from aura.quality.quality_model import QualityAxis, QualityIssue, QualitySeverity
from aura.quality.rules.misc import _issue_from_node

TERMINAL_STMTS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def check_dead_code_node(node: ast.AST) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    for field_name in ("body", "orelse", "finalbody"):
        stmts = getattr(node, field_name, None)
        if isinstance(stmts, list):
            issues.extend(_dead_code_in_block(stmts))

    if isinstance(node, ast.Try):
        for handler in node.handlers:
            issues.extend(_dead_code_in_block(handler.body))

    return issues


def _dead_code_in_block(stmts: list[ast.stmt]) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    terminal_seen = False

    for stmt in stmts:
        if terminal_seen:
            issues.append(
                _issue_from_node(
                    "dead_code",
                    "Unreachable code after return/raise/break/continue",
                    QualitySeverity.MEDIUM,
                    QualityAxis.DEAD_CODE,
                    stmt,
                    "Remove unreachable code.",
                )
            )
        elif isinstance(stmt, TERMINAL_STMTS):
            terminal_seen = True

    return issues
