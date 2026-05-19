from __future__ import annotations

import ast

from aura.quality.quality_model import QualityAxis, QualityIssue, QualitySeverity
from aura.quality.rules.misc import _issue_from_node

BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.AsyncFor,
)

CONTROL_NODES = (
    ast.If,
    ast.For,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.AsyncFor,
    ast.Try,
)


def check_complexity_node(node: ast.AST, lines: list[str]) -> list[QualityIssue]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []

    issues: list[QualityIssue] = []
    start = node.lineno
    end = getattr(node, "end_lineno", node.lineno)
    logic_lines = sum(
        1
        for line in lines[start - 1 : end]
        if line.strip() and not line.strip().startswith("#")
    )
    complexity = _cyclomatic_complexity(node)
    nesting = _max_nesting_depth(node)

    if logic_lines > 50 or complexity > 10:
        severity = QualitySeverity.HIGH if complexity > 10 else QualitySeverity.LOW
        issues.append(
            _issue_from_node(
                "god_function",
                f"Function {node.name!r} is too large or complex: {logic_lines} logic lines, complexity {complexity}",
                severity,
                QualityAxis.COMPLEXITY,
                node,
                "Break it into focused helpers or reduce branching.",
            )
        )

    if nesting > 4:
        issues.append(
            _issue_from_node(
                "deep_nesting",
                f"Function {node.name!r} has nesting depth {nesting}",
                QualitySeverity.HIGH,
                QualityAxis.COMPLEXITY,
                node,
                "Use guard clauses or extract nested logic.",
            )
        )

    if nesting >= 4 and complexity >= 5:
        issues.append(
            _issue_from_node(
                "nested_complexity",
                f"Function {node.name!r} combines nesting depth {nesting} with complexity {complexity}",
                QualitySeverity.CRITICAL,
                QualityAxis.COMPLEXITY,
                node,
                "Reduce branch count and flatten the control flow.",
            )
        )

    return issues


def _cyclomatic_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    complexity = 1

    for child in ast.walk(node):
        if isinstance(child, BRANCH_NODES):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1

    return complexity


def _max_nesting_depth(node: ast.AST, depth: int = 0) -> int:
    max_depth = depth

    if isinstance(node, CONTROL_NODES):
        depth += 1
        max_depth = depth

    for child in ast.iter_child_nodes(node):
        max_depth = max(max_depth, _max_nesting_depth(child, depth))

    return max_depth
