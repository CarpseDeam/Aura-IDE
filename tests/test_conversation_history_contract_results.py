from aura.conversation.history import History, _SOURCE_FLOOR_CHARS


def _tool_call(call_id: str, name: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    }


def test_focused_live_ruin_contract_keeps_enough_schema_to_replay() -> None:
    history = History()
    history.append_user_text("Build an old monastery")
    history.append_assistant(_tool_call("contract", "inspect_live_ruin_contract"))
    contract = "x" * 6_100
    history.append_tool_result("contract", contract)

    history._truncate_tool_results_in_range(
        0,
        len(history.messages),
        2_000,
        source_tool_min_chars=_SOURCE_FLOOR_CHARS,
    )

    assert history.messages[-1]["content"] == contract


def test_generic_tool_result_still_uses_moderate_cap() -> None:
    history = History()
    history.append_user_text("Do something")
    history.append_assistant(_tool_call("generic", "generic_tool"))
    history.append_tool_result("generic", "x" * 6_100)

    history._truncate_tool_results_in_range(
        0,
        len(history.messages),
        2_000,
        source_tool_min_chars=_SOURCE_FLOOR_CHARS,
    )

    content = history.messages[-1]["content"]
    assert len(content) < 2_500
    assert "6100 chars -> 2000 chars" in content
