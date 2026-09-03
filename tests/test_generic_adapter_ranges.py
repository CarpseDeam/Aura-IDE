"""Focused coverage for definition-node ranges in the generic tree-sitter adapter.

``GenericSymbolAdapter`` should preserve the tags query's ``definition.*``
capture node (not just the ``name`` identifier) so that ``SymbolInfo`` gets a
truthful end range whenever the bundled grammar's tags query actually
supplies one.

Not every bundled grammar's tags query exposes a definition node. The
``java`` grammar's tags query wraps the full ``class_declaration``/
``method_declaration`` node, so it is used here. The grammar is fetched by
the packaging pre-warm step into ``grammars/`` — this test does not download
anything, and skips (rather than fakes a result) if that grammar isn't
available.
"""

from __future__ import annotations

import pytest

from aura.code_intel.generic_adapter import GenericSymbolAdapter

pytestmark = pytest.mark.filterwarnings("ignore")


def _make_adapter() -> GenericSymbolAdapter | None:
    adapter = GenericSymbolAdapter("java", (".java",))
    if not adapter._lazy_init():
        return None
    if adapter._tags_query is None:
        return None
    return adapter


def test_java_definition_nodes_produce_exact_ranges() -> None:
    adapter = _make_adapter()
    if adapter is None:
        pytest.skip(
            "java tree-sitter grammar/tags query not available in this "
            "environment; cannot prove definition-node range extraction "
            "without faking it"
        )

    source = (
        "class Greeter {\n"
        "    void greet() {\n"
        "        int x = 1;\n"
        "    }\n"
        "}\n"
    )

    symbols, _, diags = adapter.parse("Greeter.java", source)
    assert not diags

    cls = next((s for s in symbols if s.name == "Greeter"), None)
    assert cls is not None, [s.name for s in symbols]
    assert cls.kind == "class"
    assert cls.line == 1
    assert cls.column == 0
    assert cls.end_line == 5
    assert cls.end_column == 1

    method = next((s for s in symbols if s.name == "greet"), None)
    assert method is not None, [s.name for s in symbols]
    assert method.kind == "method"
    # The definition node starts at "void greet()" (line 2, column 4), not the
    # bare identifier token further into the line.
    assert method.line == 2
    assert method.column == 4
    assert method.end_line == 4
    assert method.end_column == 5
