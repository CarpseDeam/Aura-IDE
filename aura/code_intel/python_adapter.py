"""Python code-intelligence adapter.

Wraps AST-based structural analysis.  The outline and symbol functions here
mirror the logic in ``aura/repo_map._outline_python`` and
``aura/conversation/tools/fs_read._outline_python`` to avoid circular
imports.  (Source: aura/repo_map.py, aura/conversation/tools/fs_read.py)
"""

from __future__ import annotations

import ast
import logging
from typing import Any

from aura.ast_utils import parse_python_ast
from aura.code_intel.adapter import CodeIntelAdapter, register_adapter

logger = logging.getLogger(__name__)

_PY_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


class PythonAdapter(CodeIntelAdapter):
    """Adapter for Python (``.py``) files using the ``ast`` module."""

    @property
    def language_id(self) -> str:
        return "python"

    @staticmethod
    def detect(file_path: str, content: str | None = None) -> bool:
        return file_path.endswith(".py")

    def parse(
        self, file_path: str, content: str
    ) -> tuple[list[Any], list[Any], list[Any]]:
        from aura.code_intel.models import ParseDiagnostic, SymbolInfo

        symbols: list[SymbolInfo] = []
        refs: list[Any] = []
        diags: list[ParseDiagnostic] = []

        try:
            tree = parse_python_ast(content, filename=file_path)
        except SyntaxError as exc:
            diags.append(
                ParseDiagnostic(
                    file=file_path,
                    line=getattr(exc, "lineno", None),
                    message=str(exc),
                    severity="error",
                )
            )
            return (symbols, refs, diags)

        # Collect symbols and references in one AST walk
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                symbols.append(
                    SymbolInfo(
                        name=node.name,
                        kind="class",
                        file=file_path,
                        line=node.lineno,
                        column=node.col_offset,
                        end_line=node.end_lineno,
                        end_column=node.end_col_offset,
                        signature="class " + node.name,
                    )
                )
                for body_node in node.body:
                    if isinstance(body_node, _PY_FUNC_TYPES):
                        symbols.append(
                            SymbolInfo(
                                name=body_node.name,
                                kind="method",
                                file=file_path,
                                line=body_node.lineno,
                                column=body_node.col_offset,
                                end_line=body_node.end_lineno,
                                end_column=body_node.end_col_offset,
                                signature=_py_func_signature_str(body_node),
                                parent=node.name,
                            )
                        )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = _py_func_signature_str(node)
                symbols.append(
                    SymbolInfo(
                        name=node.name,
                        kind="function",
                        file=file_path,
                        line=node.lineno,
                        column=node.col_offset,
                        end_line=node.end_lineno,
                        end_column=node.end_col_offset,
                        signature=sig,
                    )
                )
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.append(
                            SymbolInfo(
                                name=target.id,
                                kind="variable",
                                file=file_path,
                                line=target.lineno,
                                column=target.col_offset,
                                end_line=node.end_lineno,
                                end_column=node.end_col_offset,
                            )
                        )
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    symbols.append(
                        SymbolInfo(
                            name=node.target.id,
                            kind="variable",
                            file=file_path,
                            line=node.target.lineno,
                            column=node.target.col_offset,
                            end_line=node.end_lineno,
                            end_column=node.end_col_offset,
                        )
                    )

        refs = self.references(file_path, content)
        return (symbols, refs, diags)

    def outline(self, file_path: str, content: str) -> dict[str, Any]:
        return _outline_python_core(content, filename=file_path)

    def symbols(self, file_path: str, content: str) -> list[Any]:
        parsed, _, _ = self.parse(file_path, content)
        return parsed

    def references(self, file_path: str, content: str) -> list[Any]:
        from aura.code_intel.models import ReferenceEdge

        refs: list[ReferenceEdge] = []

        try:
            tree = parse_python_ast(content, filename=file_path)
        except SyntaxError:
            return refs

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    refs.append(
                        ReferenceEdge(
                            source_file=file_path,
                            source_symbol=None,
                            target_file=None,
                            target_symbol=alias.name,
                            line=node.lineno,
                            kind="import",
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    refs.append(
                        ReferenceEdge(
                            source_file=file_path,
                            source_symbol=None,
                            target_file=None,
                            target_symbol=f"{module}.{alias.name}",
                            line=node.lineno,
                            kind="import",
                        )
                    )

        return refs

    def dependencies(self, file_path: str, content: str) -> list[str]:
        """Resolve import statements to workspace-relative paths (best-effort).

        Only handles relative imports cleanly.  Third-party imports are
        returned as dotted module names for downstream matching.
        """
        deps: list[str] = []

        try:
            tree = parse_python_ast(content, filename=file_path)
        except SyntaxError:
            return deps

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    deps.append(alias.name.replace(".", "/") + ".py")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    # Relative imports: count leading dots
                    level = node.level or 0
                    if level > 0:
                        # Relative import — we can't resolve without knowing
                        # the file's own package path at this level.  Return
                        # module name as-is.
                        deps.append("." * level + node.module)
                    else:
                        deps.append(node.module.replace(".", "/") + ".py")

        return deps


# -- Outline helpers (mirrored from aura/repo_map.py / aura/conversation/tools/fs_read.py) --


def _outline_python_core(text: str, filename: str = "<unknown>") -> dict[str, Any]:
    """AST-based outline for Python files.

    Returns dict with keys: language, imports, classes, functions.
    Mirrors aura/repo_map._outline_python.
    """
    imports: list[str] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []

    try:
        tree = parse_python_ast(text, filename=filename)
    except SyntaxError:
        return {"language": "python", "imports": [], "classes": [], "functions": []}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if alias.asname:
                    imports.append(f"import {name} as {alias.asname}")
                else:
                    imports.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names: list[str] = []
            for alias in node.names:
                if alias.asname:
                    names.append(f"{alias.name} as {alias.asname}")
                else:
                    names.append(alias.name)
            imports.append(f"from {module} import {', '.join(names)}")
        elif isinstance(node, ast.ClassDef):
            bases = [_ast_expr_to_str(b) for b in node.bases]
            methods: list[str] = []
            for body_node in node.body:
                if isinstance(body_node, _PY_FUNC_TYPES):
                    sig = _py_func_signature_str(body_node)
                    methods.append(sig)
            classes.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "bases": bases,
                    "methods": methods,
                }
            )
        elif isinstance(node, _PY_FUNC_TYPES):
            sig = _py_func_signature_str(node)
            functions.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "signature": sig,
                }
            )

    return {
        "language": "python",
        "imports": imports,
        "classes": classes,
        "functions": functions,
    }


def _ast_expr_to_str(node: ast.expr) -> str:
    """Convert an AST expression node to a source string."""
    try:
        return ast.unparse(node)
    except (AttributeError, Exception):
        return str(type(node).__name__)


def _py_func_signature_str(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a Python function signature from AST."""
    try:
        return ast.unparse(node).split("\n")[0].rstrip(":")
    except (AttributeError, Exception):
        args = ", ".join(a.arg for a in node.args.args)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({args})"


register_adapter(PythonAdapter())
