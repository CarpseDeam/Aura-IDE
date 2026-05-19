"""Code quality scanner — AST-based analysis for AI-shaped patterns."""
from __future__ import annotations

import ast
from pathlib import Path

from aura.quality.quality_model import QualityAxis, QualityIssue, QualityReport, QualitySeverity
from aura.quality.scoring import calculate_quality_score, quality_status, sort_quality_issues
from aura.quality.rules.structural import check_structural_node, check_single_method_class
from aura.quality.rules.complexity import check_complexity_node
from aura.quality.rules.placeholder import check_placeholder_node
from aura.quality.rules.dead_code import check_dead_code_node
from aura.quality.rules.naming import scan_generic_identifier_density, scan_placeholder_variable_names
from aura.quality.rules.comments import scan_comments
from aura.quality.rules.misc import check_cross_language_node, scan_large_tuple_returns


def scan_python_quality(source: str, *, path: Path | None = None) -> QualityReport:
    """Analyze Python source code for quality issues. Never raises."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return QualityReport(
            issues=[
                QualityIssue(
                    code="syntax_error",
                    message=str(exc),
                    severity=QualitySeverity.CRITICAL,
                    axis=QualityAxis.STRUCTURE,
                    line=exc.lineno or 0,
                    col=exc.offset or 0,
                )
            ]
        )

    lines = source.splitlines()
    issues: list[QualityIssue] = []

    issues.extend(_scan_ast(tree, lines))
    issues.extend(scan_comments(source))
    issues.extend(scan_generic_identifier_density(tree))
    issues.extend(scan_placeholder_variable_names(tree))
    issues.extend(scan_large_tuple_returns(tree))

    issues = sort_quality_issues(issues)

    return QualityReport(issues=issues)


def _scan_ast(tree: ast.AST, lines: list[str]) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    for node in ast.walk(tree):
        issues.extend(check_structural_node(node, lines))
        issues.extend(check_placeholder_node(node))
        issues.extend(check_cross_language_node(node))
        issues.extend(check_single_method_class(node))
        issues.extend(check_complexity_node(node, lines))
        issues.extend(check_dead_code_node(node))

    return issues
