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


def test_class_methods_are_first_class_symbols_with_parent() -> None:
    source = (
        "class Target:\n"
        "    def method_a(self, x):\n"
        "        return x\n"
        "\n"
        "    async def method_b(self):\n"
        "        return 2\n"
    )

    syms = _symbols(source)
    kinds = {s.name: s.kind for s in syms}
    assert kinds["method_a"] == "method"
    assert kinds["method_b"] == "method"

    method_a = next(s for s in syms if s.name == "method_a")
    assert method_a.parent == "Target"
    assert method_a.line == 2
    assert method_a.column == 4
    assert method_a.end_line == 3
    assert method_a.end_column == len("        return x")
    assert method_a.signature == "def method_a(self, x)"

    method_b = next(s for s in syms if s.name == "method_b")
    assert method_b.parent == "Target"
    assert method_b.kind == "method"
    assert method_b.line == 5
    assert method_b.end_line == 6
    assert method_b.signature == "async def method_b(self)"

    # The enclosing class symbol is still present and unaffected.
    cls = next(s for s in syms if s.name == "Target")
    assert cls.kind == "class"
    assert cls.line == 1
    assert cls.end_line == 6


def test_top_level_function_has_no_parent() -> None:
    source = "def free_fn():\n    return 1\n"

    syms = _symbols(source)
    fn = next(s for s in syms if s.name == "free_fn")

    assert fn.kind == "function"
    assert fn.parent is None


def test_nested_class_methods_do_not_leak_into_unrelated_class() -> None:
    source = (
        "class First:\n"
        "    def only_in_first(self):\n"
        "        pass\n"
        "\n"
        "class Second:\n"
        "    def only_in_second(self):\n"
        "        pass\n"
    )

    syms = _symbols(source)
    first_method = next(s for s in syms if s.name == "only_in_first")
    second_method = next(s for s in syms if s.name == "only_in_second")

    assert first_method.parent == "First"
    assert second_method.parent == "Second"
