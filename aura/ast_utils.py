"""Shared AST parsing helpers for Aura's structural code analysis."""
from __future__ import annotations

import ast
import warnings


def parse_python_ast(source: str, filename: str = "<unknown>") -> ast.Module:
    """Parse Python source for internal analysis without console warning noise.

    Aura parses workspace files to build outlines and locate symbols. Those
    scans should not emit parser ``SyntaxWarning`` messages to the application
    console; the user's own Python run or test command will still surface them.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source, filename=filename)
