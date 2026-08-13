from aura.conversation.tools.catalog import ToolCatalog


def test_web_search_is_an_orthogonal_production_capability() -> None:
    catalog = ToolCatalog()
    base = {
        tool["function"]["name"]
        for tool in catalog.build_tool_defs(read_only=False, web_search=False)
    }
    with_research = {
        tool["function"]["name"]
        for tool in catalog.build_tool_defs(read_only=False, web_search=True)
    }

    assert "web_search" not in base
    assert with_research == base | {"web_search"}
