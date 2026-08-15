"""Focused tests for the ordinary OpenAI-compatible request surface."""

from __future__ import annotations

from types import SimpleNamespace

from aura.client import deepseek


def test_chat_completions_keeps_tools_but_omits_tool_choice() -> None:
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    sdk_calls: list[dict] = []
    response_chunk = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(
                reasoning_content=None,
                content="answer",
                tool_calls=None,
            ),
            finish_reason="stop",
        )],
    )

    class FakeCompletions:
        def create(self, **kwargs):
            sdk_calls.append(kwargs)
            return iter([response_chunk])

    client = deepseek.DeepSeekClient.__new__(deepseek.DeepSeekClient)
    client._provider = "openai"
    client._base_url = "https://api.openai.com/v1"
    client._chat_protocol = "openai_chat"
    client._requires_reasoning_replay = False
    client._timeout = SimpleNamespace(connect=10.0, read=None)
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    list(client.stream(
        messages=[{"role": "user", "content": "inspect source"}],
        tools=tools,
        model="gpt-test",
        thinking="high",
    ))

    assert len(sdk_calls) == 1
    assert sdk_calls[0]["tools"] == tools
    assert "tool_choice" not in sdk_calls[0]
    assert sdk_calls[0]["reasoning_effort"] == "high"
