from __future__ import annotations

import ast

from aura.quality.quality_model import QualityAxis, QualityIssue, QualitySeverity


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


def check_cross_language_node(node: ast.AST) -> list[QualityIssue]:
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
                return [_issue_from_node(code, message, QualitySeverity.HIGH, QualityAxis.CROSS_LANGUAGE, node, suggestion)]

            if attr == "Println" and isinstance(node.func.value, ast.Name) and node.func.value.id == "fmt":
                return [
                    _issue_from_node(
                        "go_println",
                        "Go pattern: use print/logging instead of fmt.Println()",
                        QualitySeverity.MEDIUM,
                        QualityAxis.CROSS_LANGUAGE,
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
                return [_issue_from_node(code, message, QualitySeverity.HIGH, QualityAxis.CROSS_LANGUAGE, node, suggestion)]

    if isinstance(node, ast.Attribute):
        patterns = {
            "length": ("js_length", "JavaScript pattern: use len() instead of .length", "Use len(value)."),
            "Length": ("csharp_length", "C# pattern: use len() instead of .Length", "Use len(value)."),
        }
        if node.attr in patterns:
            code, message, suggestion = patterns[node.attr]
            return [_issue_from_node(code, message, QualitySeverity.HIGH, QualityAxis.CROSS_LANGUAGE, node, suggestion)]

    return []


def scan_large_tuple_returns(tree: ast.AST) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            if len(node.value.elts) >= 4:
                issues.append(
                    _issue_from_node(
                        "large_tuple_return",
                        f"Return statement emits {len(node.value.elts)} anonymous values",
                        QualitySeverity.MEDIUM,
                        QualityAxis.STRUCTURE,
                        node,
                        "Use a named result dataclass or small internal model.",
                    )
                )

    return issues


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


def _issue(
    code: str,
    message: str,
    severity: QualitySeverity,
    axis: QualityAxis,
    node: ast.AST,
    lines: list[str],
    suggestion: str | None = None,
) -> QualityIssue:
    line = getattr(node, "lineno", 0)
    if 0 < line <= len(lines):
        _ = lines[line - 1].strip()  # snippet intentionally unused in new model
    return QualityIssue(
        axis=axis,
        severity=severity,
        code=code,
        message=message,
        line=line,
        col=getattr(node, "col_offset", 0),
    )


def _issue_from_node(
    code: str,
    message: str,
    severity: QualitySeverity,
    axis: QualityAxis,
    node: ast.AST,
    suggestion: str | None = None,
) -> QualityIssue:
    return QualityIssue(
        axis=axis,
        severity=severity,
        code=code,
        message=message,
        line=getattr(node, "lineno", 0),
        col=getattr(node, "col_offset", 0),
    )
