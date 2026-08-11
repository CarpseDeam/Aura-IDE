"""Focused coverage for parser-owned end ranges on Python symbols.

Python's ``ast`` module exposes truthful ``end_lineno``/``end_col_offset``
on the defining declaration node, so ``PythonAdapter`` should carry those
through onto ``SymbolInfo`` rather than leaving them ``None``.
"""

from __future__ import annotations

from aura.code_intel.python_adapter import PythonAdapter


def _symbols(source: str) -> list:
    return PythonAdapter().symbols("app.py", source)


def test_function_exact_end_range() -> None:
    source = (
        "def target_fn(x):\n"
        "    y = x + 1\n"
        "    return y\n"
    )

    syms = _symbols(source)
    fn = next(s for s in syms if s.name == "target_fn")

    assert fn.line == 1
    assert fn.column == 0
    # The function body's last line is line 3; end_col_offset points one
    # past the last character of "    return y".
    assert fn.end_line == 3
    assert fn.end_column == len("    return y")


def test_class_exact_end_range() -> None:
    source = (
        "class Target:\n"
        "    def method(self):\n"
        "        return 1\n"
    )

    syms = _symbols(source)
    cls = next(s for s in syms if s.name == "Target")

    assert cls.line == 1
    assert cls.column == 0
    assert cls.end_line == 3
    assert cls.end_column == len("        return 1")
