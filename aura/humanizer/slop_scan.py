from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from aura.humanizer.slop_model import SlopAxis, SlopIssue, SlopReport, SlopSeverity
from aura.humanizer.slop_score import calculate_slop_score, slop_status, sort_slop_issues


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

ABSTRACT_DECORATORS = {
    "abstractmethod",
    "abstractproperty",
    "abstractclassmethod",
    "abstractstaticmethod",
    "overload",
}

INTERFACE_BASES = {
    "Protocol",
    "ABC",
    "ABCMeta",
    "Interface",
    "Generic",
    "TypedDict",
    "NamedTuple",
    "Enum",
    "IntEnum",
    "StrEnum",
    "Flag",
    "IntFlag",
    "Exception",
    "BaseException",
}

SPECIAL_CLASS_DECORATORS = {
    "dataclass",
    "dataclasses.dataclass",
    "attrs",
    "attr.s",
    "attr.attrs",
    "define",
    "attr.define",
    "frozen",
    "attr.frozen",
    "runtime_checkable",
    "typing.runtime_checkable",
    "final",
    "typing.final",
}

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

TERMINAL_STMTS = (ast.Return, ast.Raise, ast.Break, ast.Continue)

NUMBERED_NAME_RE = re.compile(r"^([a-zA-Z_][a-zA-Z_]*)(\d+)$")


def scan_python_slop(source: str, *, path: Path | None = None) -> SlopReport:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return SlopReport(
            path=path,
            score=100.0,
            status="critical_deficit",
            syntax_error=str(exc),
            issues=[
                SlopIssue(
                    code="syntax_error",
                    message=str(exc),
                    severity=SlopSeverity.CRITICAL,
                    axis=SlopAxis.QUALITY,
                    line=exc.lineno or 0,
                    column=exc.offset or 0,
                )
            ],
        )

    lines = source.splitlines()
    issues: list[SlopIssue] = []

    issues.extend(_scan_ast(tree, lines))
    issues.extend(_scan_comments(source))
    issues.extend(_scan_generic_identifier_density(tree))
    issues.extend(_scan_placeholder_variable_names(tree))
    issues.extend(_scan_large_tuple_returns(tree))

    issues = sort_slop_issues(issues)
    score = calculate_slop_score(issues)

    return SlopReport(
        path=path,
        issues=issues,
        score=score,
        status=slop_status(score),
    )


def _scan_ast(tree: ast.AST, lines: list[str]) -> list[SlopIssue]:
    issues: list[SlopIssue] = []

    for node in ast.walk(tree):
        issues.extend(_check_structural_node(node, lines))
        issues.extend(_check_placeholder_node(node))
        issues.extend(_check_cross_language_node(node))
        issues.extend(_check_single_method_class(node))
        issues.extend(_check_complexity_node(node, lines))
        issues.extend(_check_dead_code_node(node))

    return issues


def _check_structural_node(node: ast.AST, lines: list[str]) -> list[SlopIssue]:
    if isinstance(node, ast.ExceptHandler) and node.type is None:
        return [
            _issue(
                "bare_except",
                "Bare except catches everything including SystemExit and KeyboardInterrupt",
                SlopSeverity.CRITICAL,
                SlopAxis.STRUCTURE,
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
                        SlopSeverity.CRITICAL,
                        SlopAxis.QUALITY,
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
                    SlopSeverity.HIGH,
                    SlopAxis.STRUCTURE,
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
                SlopSeverity.HIGH,
                SlopAxis.STRUCTURE,
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
                    SlopSeverity.CRITICAL,
                    SlopAxis.STRUCTURE,
                    node,
                    lines,
                    "Refactor to avoid dynamic code execution.",
                )
            ]

    return []


def _check_placeholder_node(node: ast.AST) -> list[SlopIssue]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _has_abstract_decorator(node):
            return []

        body = _strip_docstring(node.body)

        if len(body) == 1 and isinstance(body[0], ast.Pass):
            return [
                _issue_from_node(
                    "pass_placeholder",
                    "Function body is only pass",
                    SlopSeverity.HIGH,
                    SlopAxis.QUALITY,
                    node,
                    "Implement the function or remove it.",
                )
            ]

        if len(body) == 1 and _is_ellipsis_expr(body[0]):
            return [
                _issue_from_node(
                    "ellipsis_placeholder",
                    "Function body is only ellipsis",
                    SlopSeverity.HIGH,
                    SlopAxis.QUALITY,
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
                        SlopSeverity.HIGH,
                        SlopAxis.QUALITY,
                        node,
                        "Implement the function or make it an intentional abstract method.",
                    )
                ]

        if len(body) == 1 and isinstance(body[0], ast.Return):
            return _check_placeholder_return(node, body[0])

    if isinstance(node, ast.ExceptHandler):
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            return [_empty_except_issue(node)]

    return []


def _check_placeholder_return(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    ret: ast.Return,
) -> list[SlopIssue]:
    if node.name.startswith("__") and node.name.endswith("__"):
        return []

    if ret.value is None:
        return [
            _issue_from_node(
                "return_none_placeholder",
                "Function only returns None",
                SlopSeverity.MEDIUM,
                SlopAxis.QUALITY,
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
                    SlopSeverity.MEDIUM,
                    SlopAxis.QUALITY,
                    node,
                    "Implement the function or clarify intent.",
                )
            ]

        if node.name not in DUNDER_CONSTANT_OK:
            return [
                _issue_from_node(
                    "return_constant_stub",
                    f"Function only returns constant {ret.value.value!r}",
                    SlopSeverity.HIGH,
                    SlopAxis.QUALITY,
                    node,
                    "Implement meaningful logic or remove the stub.",
                )
            ]

    if _empty_container_repr(ret.value) is not None:
        return [
            _issue_from_node(
                "return_constant_stub",
                f"Function only returns empty {_empty_container_repr(ret.value)}",
                SlopSeverity.HIGH,
                SlopAxis.QUALITY,
                node,
                "Implement meaningful logic or remove the stub.",
            )
        ]

    return []


def _check_cross_language_node(node: ast.AST) -> list[SlopIssue]:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            patterns = {
                "push": ("js_push", "JavaScript pattern: use .append() instead of .push()", "Use .append()."),
                "equals": ("java_equals", "Java pattern: use == instead of .equals()", "Use ==."),
                "toString": ("java_tostring", "Java pattern: use str() instead of .toString()", "Use str(value)."),
                "each": ("ruby_each", "Ruby pattern: use a for loop instead of .each()", "Use a for loop."),
                "ToLower": ("csharp_tolower", "C# pattern: use .lower() instead of .ToLower()", "Use .lower()."),
            }
            if attr in patterns:
                code, message, suggestion = patterns[attr]
                return [_issue_from_node(code, message, SlopSeverity.HIGH, SlopAxis.QUALITY, node, suggestion)]

            if attr == "Println" and isinstance(node.func.value, ast.Name) and node.func.value.id == "fmt":
                return [
                    _issue_from_node(
                        "go_println",
                        "Go pattern: use print/logging instead of fmt.Println()",
                        SlopSeverity.MEDIUM,
                        SlopAxis.QUALITY,
                        node,
                        "Use print() or logging.",
                    )
                ]

        if isinstance(node.func, ast.Name):
            patterns = {
                "strlen": ("php_strlen", "PHP pattern: use len() instead of strlen()", "Use len(value)."),
                "array_push": ("php_array_push", "PHP pattern: use .append() instead of array_push()", "Use list.append(value)."),
            }
            if node.func.id in patterns:
                code, message, suggestion = patterns[node.func.id]
                return [_issue_from_node(code, message, SlopSeverity.HIGH, SlopAxis.QUALITY, node, suggestion)]

    if isinstance(node, ast.Attribute):
        patterns = {
            "length": ("js_length", "JavaScript pattern: use len() instead of .length", "Use len(value)."),
            "Length": ("csharp_length", "C# pattern: use len() instead of .Length", "Use len(value)."),
        }
        if node.attr in patterns:
            code, message, suggestion = patterns[node.attr]
            return [_issue_from_node(code, message, SlopSeverity.HIGH, SlopAxis.QUALITY, node, suggestion)]

    return []


def _check_single_method_class(node: ast.AST) -> list[SlopIssue]:
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
                SlopSeverity.HIGH,
                SlopAxis.STRUCTURE,
                node,
                "Use a function unless the class has real state or a clear domain role.",
            )
        ]

    return []


def _check_complexity_node(node: ast.AST, lines: list[str]) -> list[SlopIssue]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []

    issues: list[SlopIssue] = []
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
        severity = SlopSeverity.HIGH if complexity > 10 else SlopSeverity.LOW
        issues.append(
            _issue_from_node(
                "god_function",
                f"Function {node.name!r} is too large or complex: {logic_lines} logic lines, complexity {complexity}",
                severity,
                SlopAxis.STYLE,
                node,
                "Break it into focused helpers or reduce branching.",
            )
        )

    if nesting > 4:
        issues.append(
            _issue_from_node(
                "deep_nesting",
                f"Function {node.name!r} has nesting depth {nesting}",
                SlopSeverity.HIGH,
                SlopAxis.STYLE,
                node,
                "Use guard clauses or extract nested logic.",
            )
        )

    if nesting >= 4 and complexity >= 5:
        issues.append(
            _issue_from_node(
                "nested_complexity",
                f"Function {node.name!r} combines nesting depth {nesting} with complexity {complexity}",
                SlopSeverity.CRITICAL,
                SlopAxis.QUALITY,
                node,
                "Reduce branch count and flatten the control flow.",
            )
        )

    return issues


def _check_dead_code_node(node: ast.AST) -> list[SlopIssue]:
    issues: list[SlopIssue] = []

    for field_name in ("body", "orelse", "finalbody"):
        stmts = getattr(node, field_name, None)
        if isinstance(stmts, list):
            issues.extend(_dead_code_in_block(stmts))

    if isinstance(node, ast.Try):
        for handler in node.handlers:
            issues.extend(_dead_code_in_block(handler.body))

    return issues


def _dead_code_in_block(stmts: list[ast.stmt]) -> list[SlopIssue]:
    issues: list[SlopIssue] = []
    terminal_seen = False

    for stmt in stmts:
        if terminal_seen:
            issues.append(
                _issue_from_node(
                    "dead_code",
                    "Unreachable code after return/raise/break/continue",
                    SlopSeverity.MEDIUM,
                    SlopAxis.QUALITY,
                    stmt,
                    "Remove unreachable code.",
                )
            )
        elif isinstance(stmt, TERMINAL_STMTS):
            terminal_seen = True

    return issues


def _scan_large_tuple_returns(tree: ast.AST) -> list[SlopIssue]:
    issues: list[SlopIssue] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            if len(node.value.elts) >= 4:
                issues.append(
                    _issue_from_node(
                        "large_tuple_return",
                        f"Return statement emits {len(node.value.elts)} anonymous values",
                        SlopSeverity.MEDIUM,
                        SlopAxis.STRUCTURE,
                        node,
                        "Use a named result dataclass or small internal model.",
                    )
                )

    return issues


def _scan_generic_identifier_density(tree: ast.AST) -> list[SlopIssue]:
    issues: list[SlopIssue] = []

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
                    SlopSeverity.MEDIUM,
                    SlopAxis.STYLE,
                    node,
                    "Use names that reflect the domain and responsibility.",
                )
            )

    return issues


def _scan_placeholder_variable_names(tree: ast.AST) -> list[SlopIssue]:
    issues: list[SlopIssue] = []

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
                    SlopSeverity.HIGH,
                    SlopAxis.QUALITY,
                    node,
                    "Use semantic parameter names.",
                )
            )

        numbered = _collect_numbered_vars(node)
        for prefix, nums in numbered.items():
            run = _max_consecutive_run(sorted(set(nums)))
            if run >= 4:
                severity = SlopSeverity.HIGH if run >= 8 else SlopSeverity.MEDIUM
                issues.append(
                    _issue_from_node(
                        "placeholder_variable_naming",
                        f"Function {node.name!r} uses sequential numbered variables like {prefix}1..{prefix}{run}",
                        severity,
                        SlopAxis.QUALITY,
                        node,
                        "Use descriptive names or a list/dict.",
                    )
                )
                break

    return issues


def _scan_comments(source: str) -> list[SlopIssue]:
    issues: list[SlopIssue] = []
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
                    SlopIssue(
                        code="redundant_comment",
                        message="Comment restates obvious code",
                        severity=SlopSeverity.MEDIUM,
                        axis=SlopAxis.NOISE,
                        line=line,
                        column=column,
                        snippet=token.string,
                        suggestion="Remove narration comments.",
                    )
                )

            if re.match(r"^(assuming|assumes|presumably|apparently|i think|we think|should be|might be)\b", lowered):
                issues.append(
                    SlopIssue(
                        code="assumption_comment",
                        message="Comment signals unverified behavior",
                        severity=SlopSeverity.HIGH,
                        axis=SlopAxis.QUALITY,
                        line=line,
                        column=column,
                        snippet=token.string,
                        suggestion="Verify behavior or remove the claim.",
                    )
                )

            if re.match(r"^(should work|hopefully|probably|might work|try this|seems to|appears to)\b", lowered):
                issues.append(
                    SlopIssue(
                        code="hedging_comment",
                        message="Hedging comment suggests uncertainty",
                        severity=SlopSeverity.HIGH,
                        axis=SlopAxis.STYLE,
                        line=line,
                        column=column,
                        snippet=token.string,
                        suggestion="Fix or verify the behavior instead.",
                    )
                )

            if re.match(r"^(obviously|clearly|simply|just|easy|trivial|basically|of course|naturally)\b", lowered):
                issues.append(
                    SlopIssue(
                        code="overconfident_comment",
                        message="Overconfident comment adds noise",
                        severity=SlopSeverity.MEDIUM,
                        axis=SlopAxis.STYLE,
                        line=line,
                        column=column,
                        snippet=token.string,
                        suggestion="Explain constraints, not obviousness.",
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


def _has_abstract_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        name = _decorator_name(decorator)
        if name in ABSTRACT_DECORATORS:
            return True
    return False


def _decorator_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


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


def _empty_except_issue(node: ast.ExceptHandler) -> SlopIssue:
    if node.type is None:
        severity = SlopSeverity.CRITICAL
        message = "Bare except pass silently swallows every exception"
        suggestion = "Catch specific exception types and handle or log them."
    elif _is_import_guard_exception(node.type):
        severity = SlopSeverity.LOW
        message = "Optional dependency guard uses pass"
        suggestion = "Add a short comment explaining the fallback behavior."
    else:
        severity = SlopSeverity.MEDIUM
        message = "Typed exception handler silently discards the exception"
        suggestion = "Log, handle, or explain why silence is correct."

    return _issue_from_node(
        "empty_except",
        message,
        severity,
        SlopAxis.QUALITY,
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


def _is_interface_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        base_name = _base_name(base)
        if base_name in INTERFACE_BASES:
            return True

    for keyword in node.keywords:
        if keyword.arg == "metaclass":
            meta_name = _base_name(keyword.value)
            if meta_name in INTERFACE_BASES:
                return True

    return False


def _has_special_class_decorator(node: ast.ClassDef) -> bool:
    return any(_decorator_name(decorator) in SPECIAL_CLASS_DECORATORS for decorator in node.decorator_list)


def _has_significant_base(node: ast.ClassDef) -> bool:
    for base in node.bases:
        base_name = _base_name(base)
        if base_name and base_name != "object":
            return True
    return False


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return None


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


def _issue(
    code: str,
    message: str,
    severity: SlopSeverity,
    axis: SlopAxis,
    node: ast.AST,
    lines: list[str],
    suggestion: str | None = None,
) -> SlopIssue:
    line = getattr(node, "lineno", 0)
    snippet = lines[line - 1].strip() if 0 < line <= len(lines) else None

    return SlopIssue(
        code=code,
        message=message,
        severity=severity,
        axis=axis,
        line=line,
        column=getattr(node, "col_offset", 0),
        snippet=snippet,
        suggestion=suggestion,
    )


def _issue_from_node(
    code: str,
    message: str,
    severity: SlopSeverity,
    axis: SlopAxis,
    node: ast.AST,
    suggestion: str | None = None,
) -> SlopIssue:
    return SlopIssue(
        code=code,
        message=message,
        severity=severity,
        axis=axis,
        line=getattr(node, "lineno", 0),
        column=getattr(node, "col_offset", 0),
        suggestion=suggestion,
    )