"""Focused coverage for definition-node ranges in the generic tree-sitter adapter.

``GenericSymbolAdapter`` should preserve the tags query's ``definition.*``
capture node (not just the ``name`` identifier) so that ``SymbolInfo`` gets a
truthful end range whenever the bundled grammar's tags query actually
supplies one.

Not every bundled grammar's tags query exposes a definition node — e.g. the
``gdscript`` tags query bundled with this environment has no
``definition.*`` capture at all, so it cannot prove this behavior. The
``gdshader`` grammar's tags query does wrap the full ``function_definition``/
``struct_definition`` node, so it is used here instead. Both grammars are
pre-warmed on disk under ``grammars/`` for this dev environment already —
this test does not download anything, and skips (rather than fakes a
result) if that grammar isn't available.
"""

from __future__ import annotations

import pytest

from aura.code_intel.generic_adapter import GenericSymbolAdapter

pytestmark = pytest.mark.filterwarnings("ignore")


def _make_adapter() -> GenericSymbolAdapter | None:
    adapter = GenericSymbolAdapter("gdshader", (".gdshader",))
    if not adapter._lazy_init():
        return None
    if adapter._tags_query is None:
        return None
    return adapter


def test_gdshader_definition_nodes_produce_exact_ranges() -> None:
    adapter = _make_adapter()
    if adapter is None:
        pytest.skip(
            "gdshader tree-sitter grammar/tags query not available in this "
            "environment; cannot prove definition-node range extraction "
            "without faking it"
        )

    source = (
        "shader_type canvas_item;\n"
        "\n"
        "void fragment() {\n"
        "    COLOR = vec4(1.0);\n"
        "}\n"
        "\n"
        "struct Foo {\n"
        "    int x;\n"
        "};\n"
    )

    symbols, _, diags = adapter.parse("shader.gdshader", source)
    assert not diags

    fn = next((s for s in symbols if s.name == "fragment"), None)
    assert fn is not None, [s.name for s in symbols]
    assert fn.kind == "function"
    # The definition node starts at "void fragment()" (line 3), not the
    # bare identifier token further into the line.
    assert fn.line == 3
    assert fn.column == 0
    assert fn.end_line == 5
    assert fn.end_column == 1

    struct = next((s for s in symbols if s.name == "Foo"), None)
    assert struct is not None, [s.name for s in symbols]
    assert struct.kind == "class"
    assert struct.line == 7
    assert struct.column == 0
    assert struct.end_line == 9
    assert struct.end_column == 2
