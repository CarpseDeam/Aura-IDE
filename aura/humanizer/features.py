from __future__ import annotations

import ast
import tokenize
from dataclasses import dataclass, field
from io import StringIO

_GENERIC_NAMES = {
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
}

_NARRATION_TRIGGERS = [
    "Initialize",
    "Loop through",
    "Iterate through",
    "Check if",
    "Process each",
    "Return the",
    "Build set",
    "Build a",
    "Create a",
    "Calculate",
    "Update the",
    "Iterate over",
    "Define the",
    "Set up",
    "Get the",
    "Store the",
    "Append the",
    "Call the",
    "Handle the",
    "Convert the",
    "Load the",
    "Save the",
]

_NARRATION_EXCLUDE_SUBSTRINGS = [
    "TODO",
    "FIXME",
    "NOTE",
    "WARNING",
    "HACK",
    "BUG",
    "XXX",
    "noqa",
    "type:",
    "pyright:",
    "mypy:",
    "ruff:",
    "http://",
    "https://",
    "copyright",
    "license",
    "author",
]


@dataclass
class TupleReturnHit:
    function_name: str
    line: int
    size: int


@dataclass
class GenericNameHit:
    name: str
    line: int


@dataclass
class NarrationCommentHit:
    text: str
    line: int


@dataclass
class ThinHelperHit:
    function_name: str
    line: int
    body_lines: int


@dataclass
class CodeFeatureReport:
    tuple_returns: list[TupleReturnHit] = field(default_factory=list)
    generic_names: list[GenericNameHit] = field(default_factory=list)
    narration_comments: list[NarrationCommentHit] = field(default_factory=list)
    thin_helpers: list[ThinHelperHit] = field(default_factory=list)

    @property
    def has_structural_smells(self) -> bool:
        return bool(
            self.tuple_returns
            or self.generic_names
            or self.narration_comments
            or self.thin_helpers
        )


def analyze_python_features(source: str) -> CodeFeatureReport:
    """Analyze Python code for AI-shaped patterns. Never raises."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return CodeFeatureReport()

    try:
        report = CodeFeatureReport()
        report.tuple_returns = _detect_large_tuple_returns(tree)
        report.generic_names = _detect_generic_names(source, tree)
        report.narration_comments = _detect_narration_comments(source)
        report.thin_helpers = _detect_thin_helpers(tree)
        return report
    except Exception:
        return CodeFeatureReport()


def _detect_large_tuple_returns(tree: ast.AST) -> list[TupleReturnHit]:
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent

    hits: list[TupleReturnHit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return):
            continue
        if not isinstance(node.value, ast.Tuple):
            continue
        elts = node.value.elts
        if len(elts) < 4:
            continue

        func_name = _enclosing_function_name(node, parent_map)
        hits.append(
            TupleReturnHit(
                function_name=func_name,
                line=node.lineno,
                size=len(elts),
            )
        )
    return hits


def _enclosing_function_name(
    node: ast.Return, parent_map: dict[int, ast.AST]
) -> str:
    current = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parent_map.get(id(current))
    return "<unknown>"


def _detect_generic_names(
    source: str, tree: ast.AST
) -> list[GenericNameHit]:
    seen: set[tuple[str, int]] = set()
    hits: list[GenericNameHit] = []

    for node in ast.walk(tree):
        targets: list[ast.Name] = []

        if isinstance(node, ast.Assign):
            for target in node.targets:
                _collect_names(target, targets)
        elif isinstance(node, ast.AugAssign):
            _collect_names(node.target, targets)
        elif isinstance(node, ast.NamedExpr):
            _collect_names(node.target, targets)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.posonlyargs:
                if isinstance(arg.arg, str):
                    key = (arg.arg, arg.lineno)
                    if key not in seen and arg.arg in _GENERIC_NAMES:
                        seen.add(key)
                        hits.append(GenericNameHit(name=arg.arg, line=arg.lineno))
                elif isinstance(arg, ast.Name) and arg.id in _GENERIC_NAMES:
                    key = (arg.id, arg.lineno)
                    if key not in seen:
                        seen.add(key)
                        hits.append(GenericNameHit(name=arg.id, line=arg.lineno))
            continue

        for name_node in targets:
            key = (name_node.id, name_node.lineno)
            if key not in seen and name_node.id in _GENERIC_NAMES:
                seen.add(key)
                hits.append(
                    GenericNameHit(name=name_node.id, line=name_node.lineno)
                )

    return hits


def _collect_names(node: ast.AST, out: list[ast.Name]) -> None:
    if isinstance(node, ast.Name):
        out.append(node)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _collect_names(elt, out)
    elif isinstance(node, ast.Starred):
        _collect_names(node.value, out)


def _detect_narration_comments(source: str) -> list[NarrationCommentHit]:
    hits: list[NarrationCommentHit] = []
    try:
        tokens = tokenize.generate_tokens(StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            text = tok.string
            body = text.lstrip("#").lstrip()
            if not body:
                continue

            lower_body = body.lower()
            if _is_narration_excluded(lower_body):
                continue

            if _starts_with_trigger(body):
                hits.append(
                    NarrationCommentHit(text=text.strip(), line=tok.start[0])
                )
    except tokenize.TokenError:
        pass
    return hits


def _is_narration_excluded(lower_body: str) -> bool:
    for substr in _NARRATION_EXCLUDE_SUBSTRINGS:
        if substr.lower() in lower_body:
            return True
    return False


def _starts_with_trigger(body: str) -> bool:
    lower = body.lower()
    for trigger in _NARRATION_TRIGGERS:
        if lower.startswith(trigger.lower()):
            return True
    return False


def _detect_thin_helpers(tree: ast.AST) -> list[ThinHelperHit]:
    hits: list[ThinHelperHit] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_"):
            continue
        if node.name.startswith("__") and node.name.endswith("__"):
            continue
        if not node.body:
            continue
        body_lines = node.body[-1].end_lineno - node.body[0].lineno + 1
        if body_lines <= 3:
            hits.append(
                ThinHelperHit(
                    function_name=node.name,
                    line=node.lineno,
                    body_lines=body_lines,
                )
            )
    return hits
