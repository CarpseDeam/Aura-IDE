"""Focused offline contracts for DeepSeek V4 Responses production turns."""

from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from aura.backends.api import APIAgentBackend
from aura.client import deepseek as ds
from aura.client.events import ApiError, Done, ReasoningDelta, ToolCallEnd, ToolCallStart, Usage
from aura.client.responses_stream import (
    DeepSeekResponsesStreamParser,
    build_deepseek_responses_request,
    project_deepseek_responses_input,
)
from aura.conversation import ConversationManager, History
from aura.conversation.tools import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry


def _response_event(response: Any) -> SimpleNamespace:
    return SimpleNamespace(type="response.completed", response=response)


def _response_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=120,
        output_tokens=32,
        total_tokens=152,
        input_tokens_details=SimpleNamespace(cached_tokens=80),
    )


def _completed_response(*output: Any) -> SimpleNamespace:
    return SimpleNamespace(id="resp-test", output=list(output), usage=_usage())


def _contains_reasoning(value: Any) -> bool:
    if isinstance(value, dict):
        return (
            value.get("type") == "reasoning"
            or "reasoning_content" in value
            or any(_contains_reasoning(item) for item in value.values())
        )
    if isinstance(value, list):
        return any(_contains_reasoning(item) for item in value)
    return False


def test_projection_is_stateless_ordered_and_does_not_mutate_history() -> None:
    messages = [
        {"role": "system", "content": "system", "aura_internal": True},
        {"role": "user", "content": "edit note.txt", "local": "drop"},
        {
            "role": "assistant",
            "content": "I will edit it.",
            "reasoning_content": "private prior reasoning",
            "reasoning_signature": "foreign signature",
            "tool_calls": [{
                "id": "call-provider-1",
                "type": "function",
                "provider_output_item_id": "fc-foreign",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path":"note.txt","content":"done"}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-provider-1",
            "content": '{"ok":true}',
            "aura_internal": True,
        },
        {"role": "assistant", "content": "", "reasoning_content": "only old CoT"},
    ]
    before = copy.deepcopy(messages)

    instructions, items = project_deepseek_responses_input(messages)

    assert instructions == "system"
    assert [item.get("type", item.get("role")) for item in items] == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
    ]
    assert items[2] == {
        "type": "function_call",
        "call_id": "call-provider-1",
        "name": "write_file",
        "arguments": '{"path":"note.txt","content":"done"}',
    }
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "call-provider-1",
        "output": '{"ok":true}',
    }
    assert _contains_reasoning(items) is False
    assert messages == before


def test_history_stays_canonical_when_request_is_projected() -> None:
    history = History()
    history.set_system("system")
    history.append_user_text("continue")
    history.append_assistant({
        "role": "assistant",
        "content": "visible",
        "reasoning_content": "retain in canonical history",
    })
    before = copy.deepcopy(history.messages)

    request = build_deepseek_responses_request(
        messages=history.for_api(),
        tools=None,
        model="deepseek-v4-flash",
        thinking="max",
    )

    assert history.messages == before
    assert _contains_reasoning(request["input"]) is False


@pytest.mark.parametrize("mode", ["off", "high", "max"])
def test_responses_thinking_mapping_and_temperature(mode: str) -> None:
    request = build_deepseek_responses_request(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model="deepseek-v4-flash",
        thinking=mode,
        temperature=0.25,
    )

    assert request["reasoning"] == {"effort": mode}
    assert (request.get("temperature") == 0.25) is (mode == "off")
    assert "tool_choice" not in request
    assert "previous_response_id" not in request
    assert "conversation" not in request


def test_responses_request_translates_tools_and_keeps_append_only_prefix() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {}},
            "local_metadata": "drop",
        },
    }
    first_messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "go"}]
    second_messages = first_messages + [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "omit me",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"x"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "x"},
    ]

    first = build_deepseek_responses_request(
        messages=first_messages,
        tools=[tool],
        model="deepseek-v4-flash",
        thinking="max",
    )
    second = build_deepseek_responses_request(
        messages=second_messages,
        tools=[tool],
        model="deepseek-v4-flash",
        thinking="max",
    )

    assert first["input"] == second["input"][: len(first["input"])]
    assert second["tools"] == [{
        "type": "function",
        "name": "read_file",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {}},
    }]
    assert _contains_reasoning(second) is False


def test_responses_parser_emits_reasoning_usage_and_call_id_not_item_id() -> None:
    parser = DeepSeekResponsesStreamParser()
    item = SimpleNamespace(
        type="function_call",
        id="fc-provider-output-item",
        call_id="call-aura-7",
        name="read_file",
        arguments="",
    )
    events = []
    events.extend(parser.push(SimpleNamespace(type="response.reasoning_text.delta", delta="plan")))
    events.extend(parser.push(SimpleNamespace(type="response.reasoning_text.done", text="plan")))
    events.extend(parser.push(SimpleNamespace(type="response.output_item.added", output_index=0, item=item)))
    events.extend(parser.push(SimpleNamespace(
        type="response.function_call_arguments.delta", output_index=0, delta='{"path":'
    )))
    events.extend(parser.push(SimpleNamespace(
        type="response.function_call_arguments.done", output_index=0, arguments='{"path":"x"}'
    )))
    events.extend(parser.push(SimpleNamespace(
        type="response.output_item.done", output_index=0, item={**vars(item), "arguments": '{"path":"x"}'},
    )))
    events.extend(parser.push(_response_event(_completed_response(item))))

    assert [event.text for event in events if isinstance(event, ReasoningDelta)] == ["plan"]
    starts = [event for event in events if isinstance(event, ToolCallStart)]
    assert starts[0].id == "call-aura-7"
    assert len([event for event in events if isinstance(event, ToolCallEnd)]) == 1
    usage = [event for event in events if isinstance(event, Usage)]
    assert usage[0].cache_hit_tokens == 80
    done_message = parser.full_message()
    assert done_message["reasoning_content"] == "plan"
    assert done_message["tool_calls"][0]["id"] == "call-aura-7"
    assert "fc-provider-output-item" not in json.dumps(done_message)
    assert parser.finish_reason == "tool_calls"


def test_responses_parser_keeps_parallel_calls_in_output_order() -> None:
    parser = DeepSeekResponsesStreamParser()
    calls = [
        SimpleNamespace(
            type="function_call",
            id="fc-1",
            call_id="call-1",
            name="read_file",
            arguments='{"path":"a"}',
        ),
        SimpleNamespace(
            type="function_call",
            id="fc-2",
            call_id="call-2",
            name="read_file",
            arguments='{"path":"b"}',
        ),
    ]
    for index, call in enumerate(calls):
        parser.push(SimpleNamespace(type="response.output_item.added", output_index=index, item=call))
        parser.push(SimpleNamespace(type="response.output_item.done", output_index=index, item=call))
    parser.push(_response_event(_completed_response(*calls)))

    assert [call["id"] for call in parser.full_message()["tool_calls"]] == ["call-1", "call-2"]


@pytest.mark.parametrize(
    ("terminal_type", "response"),
    [
        (
            "response.incomplete",
            SimpleNamespace(
                id="resp-incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                usage=_usage(),
            ),
        ),
        (
            "response.failed",
            SimpleNamespace(
                id="resp-failed",
                error=SimpleNamespace(code="server_error", message="failed"),
            ),
        ),
    ],
)
def test_incomplete_and_failed_terminal_events_do_not_make_successful_done(
    terminal_type: str, response: Any
) -> None:
    class Responses:
        def create(self, **_kwargs):
            return iter([SimpleNamespace(type=terminal_type, response=response)])

    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = "deepseek"
    client._client = SimpleNamespace(responses=Responses())
    events = list(client.stream(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model="deepseek-v4-flash",
        thinking="max",
    ))

    assert any(isinstance(event, ApiError) for event in events)
    assert not any(isinstance(event, Done) for event in events)


def test_v4_routes_to_responses_without_chat_fallback() -> None:
    calls: list[dict[str, Any]] = []
    chat_calls: list[dict[str, Any]] = []

    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return iter([
                _response_event(_completed_response(_response_message("ok"))),
            ])

    class ChatCompletions:
        def create(self, **kwargs):
            chat_calls.append(kwargs)
            return iter(())

    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = "deepseek"
    client._chat_protocol = "openai_chat"
    client._timeout = SimpleNamespace(connect=10.0, read=None)
    client._client = SimpleNamespace(
        responses=Responses(),
        chat=SimpleNamespace(completions=ChatCompletions()),
    )

    events = list(client.stream(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model="deepseek-v4-flash",
        thinking="max",
    ))

    assert len(calls) == 1
    assert chat_calls == []
    assert calls[0]["reasoning"] == {"effort": "max"}
    assert "tool_choice" not in calls[0]
    assert isinstance([event for event in events if isinstance(event, Done)][0], Done)


def test_v4_responses_failure_does_not_retry_chat() -> None:
    chat_calls: list[dict[str, Any]] = []

    class Responses:
        def create(self, **_kwargs):
            raise RuntimeError("responses unavailable")

    class ChatCompletions:
        def create(self, **kwargs):
            chat_calls.append(kwargs)
            return iter(())

    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = "deepseek"
    client._chat_protocol = "openai_chat"
    client._client = SimpleNamespace(
        responses=Responses(),
        chat=SimpleNamespace(completions=ChatCompletions()),
    )

    events = list(client.stream(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model="deepseek-v4-flash",
        thinking="max",
    ))

    assert len([event for event in events if isinstance(event, ApiError)]) == 1
    assert chat_calls == []


def test_responses_watchdog_and_cancellation_never_execute_partial_calls(monkeypatch) -> None:
    class StalledStream:
        def __iter__(self):
            yield SimpleNamespace(type="response.created", response=SimpleNamespace(id="r"))
            threading.Event().wait(30)

        def close(self):
            return None

    class Responses:
        def create(self, **_kwargs):
            return StalledStream()

    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = "deepseek"
    client._client = SimpleNamespace(responses=Responses())
    monkeypatch.setattr(ds, "FIRST_STREAM_EVENT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(ds, "RESPONSES_INTER_EVENT_TIMEOUT_SECONDS", 0.0)

    stalled = list(client.stream(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model="deepseek-v4-flash",
        thinking="max",
    ))
    assert any(isinstance(event, ApiError) for event in stalled)
    assert not any(isinstance(event, Done) for event in stalled)

    cancel = threading.Event()
    cancel.set()
    cancelled = list(client.stream(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model="deepseek-v4-flash",
        thinking="max",
        cancel_event=cancel,
    ))
    assert not any(isinstance(event, ApiError) for event in cancelled)
    assert any(isinstance(event, Done) for event in cancelled)


def test_conversation_manager_pairs_provider_call_id_across_tool_round(tmp_path, monkeypatch) -> None:
    (tmp_path / "note.txt").write_text("before", encoding="utf-8")
    requests: list[dict[str, Any]] = []

    call_item = SimpleNamespace(
        type="function_call",
        id="fc-foreign-item",
        call_id="call-provider-42",
        name="read_file",
        arguments='{"path":"note.txt"}',
    )
    first_events = [
        SimpleNamespace(type="response.reasoning_text.delta", delta="private plan"),
        SimpleNamespace(type="response.output_item.added", output_index=0, item=call_item),
        SimpleNamespace(
            type="response.function_call_arguments.done",
            output_index=0,
            arguments='{"path":"note.txt"}',
        ),
        SimpleNamespace(type="response.output_item.done", output_index=0, item=call_item),
        _response_event(_completed_response(call_item)),
    ]
    second_events = [_response_event(_completed_response(_response_message("validated")))]

    class Responses:
        def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            return iter(first_events if len(requests) == 1 else second_events)

    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = "deepseek"
    client._client = SimpleNamespace(responses=Responses())
    backend = APIAgentBackend(provider="deepseek")
    backend._client = client
    registry = ModelStreamRegistry()
    registry.register(PRODUCTION_STREAM_HOOK, backend.stream)
    monkeypatch.setattr("aura.conversation.manager.model_streams", registry)

    history = History()
    history.set_system("system")
    history.append_user_text("Read note.txt and validate it.")
    manager = ConversationManager(history, ToolRegistry(tmp_path))
    manager.send(
        on_event=lambda _event: None,
        approval_cb=lambda _request: None,
        cancel_event=threading.Event(),
        model="deepseek-v4-flash",
        thinking="max",
    )

    assert len(requests) == 2
    second_input = requests[1]["input"]
    assert {item.get("type") for item in second_input} >= {
        "function_call",
        "function_call_output",
    }
    function_call = next(item for item in second_input if item.get("type") == "function_call")
    function_output = next(item for item in second_input if item.get("type") == "function_call_output")
    assert function_call["call_id"] == "call-provider-42"
    assert function_output["call_id"] == "call-provider-42"
    assert _contains_reasoning(requests[1]["input"]) is False
    assert history.messages[-1]["content"] == "validated"
    assert history.messages[-2]["role"] == "tool"
    assert history.messages[-2]["tool_call_id"] == "call-provider-42"
