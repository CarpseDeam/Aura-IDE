from __future__ import annotations

import ast
import re

from aura.quality.quality_model import QualityAxis, QualityIssue, QualitySeverity
from aura.quality.rules.misc import _issue_from_node

GENERIC_NAMES = {
    "data",
    "result",
    "results",
    "item",
    "items",
    "value",
    "values",
    "output",
    "outputs",
    "processed",
    "valid_items",
    "file_info",
    "info",
    "obj",
    "temp",
    "tmp",
}

NUMBERED_NAME_RE = re.compile(r"^([a-zA-Z_][a-zA-Z_]*)(\d+)$")


def scan_generic_identifier_density(tree: ast.AST) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        hits: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                if child.id in GENERIC_NAMES:
                    hits.append(child.id)
            elif isinstance(child, ast.arg):
                if child.arg in GENERIC_NAMES:
                    hits.append(child.arg)

        if len(hits) >= 5:
            preview = ", ".join(sorted(set(hits))[:8])
            issues.append(
                _issue_from_node(
                    "generic_identifier_density",
                    f"Function {node.name!r} uses too many generic names: {preview}",
                    QualitySeverity.MEDIUM,
                    QualityAxis.NAMING,
                    node,
                    "Use names that reflect the domain and responsibility.",
                )
            )

    return issues


def scan_placeholder_variable_names(tree: ast.AST) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        args = (
            [arg.arg for arg in node.args.args]
            + [arg.arg for arg in node.args.posonlyargs]
            + [arg.arg for arg in node.args.kwonlyargs]
        )
        semantic_args = [arg for arg in args if arg not in {"self", "cls", "_"}]
        single_letter = [arg for arg in semantic_args if len(arg) == 1]

        if len(single_letter) >= 5:
            issues.append(
                _issue_from_node(
                    "placeholder_variable_naming",
                    f"Function {node.name!r} has {len(single_letter)} single-letter parameters",
                    QualitySeverity.HIGH,
                    QualityAxis.NAMING,
                    node,
                    "Use semantic parameter names.",
                )
            )

        numbered = _collect_numbered_vars(node)
        for prefix, nums in numbered.items():
            run = _max_consecutive_run(sorted(set(nums)))
            if run >= 4:
                severity = QualitySeverity.HIGH if run >= 8 else QualitySeverity.MEDIUM
                issues.append(
                    _issue_from_node(
                        "placeholder_variable_naming",
                        f"Function {node.name!r} uses sequential numbered variables like {prefix}1..{prefix}{run}",
                        severity,
                        QualityAxis.NAMING,
                        node,
                        "Use descriptive names or a list/dict.",
                    )
                )
                break

    return issues


def _collect_numbered_vars(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, list[int]]:
    numbered: dict[str, list[int]] = {}

    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue

        for target in child.targets:
            if not isinstance(target, ast.Name):
                continue

            match = NUMBERED_NAME_RE.match(target.id)
            if not match:
                continue

            prefix, number = match.group(1), int(match.group(2))
            numbered.setdefault(prefix, []).append(number)

    return numbered


def _max_consecutive_run(nums: list[int]) -> int:
    if not nums:
        return 0

    best = current = 1
    for index in range(1, len(nums)):
        if nums[index] == nums[index - 1] + 1:
            current += 1
            best = max(best, current)
        else:
            current = 1

    return best
