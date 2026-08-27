"""Offline contracts for the shared production Responses parser."""

from __future__ import annotations

from types import SimpleNamespace

from aura.client.deepseek_responses import ResponsesProductionStreamParser
from aura.client.events import ContentDelta, ToolCallEnd, ToolCallStart, Usage
from aura.client.hosted_search import AURA_HOSTED_SEARCH_KEY


def _usage():
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_tokens_details=SimpleNamespace(cached_tokens=2),
    )


def _citation(url: str = "https://example.com/source"):
    return SimpleNamespace(type="url_citation", url=url, title="Example")


def _message(text: str = "answer", annotations=None):
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(
            type="output_text",
            text=text,
            annotations=list(annotations or []),
        )],
    )


def _completed(*output):
    return SimpleNamespace(id="resp-1", output=list(output), usage=_usage())


def test_hosted_search_activity_never_emits_client_tool_events() -> None:
    parser = ResponsesProductionStreamParser(
        provider="openai",
        hosted_tool_type="web_search",
    )
    search = SimpleNamespace(
        type="web_search_call",
        id="ws-1",
        status="completed",
        action=SimpleNamespace(type="search", queries=["Aura current release"]),
    )

    events = []
    events.extend(parser.push(SimpleNamespace(
        type="response.output_item.added", output_index=0, item=search
    )))
    events.extend(parser.push(SimpleNamespace(
        type="response.web_search_call.completed", item_id="ws-1"
    )))
    events.extend(parser.push(SimpleNamespace(
        type="response.output_item.done", output_index=0, item=search
    )))

    assert not any(isinstance(event, (ToolCallStart, ToolCallEnd)) for event in events)
    assert parser.web_search_calls == [{
        "item_id": "ws-1",
        "status": "completed",
        "action": "search",
        "queries": ["Aura current release"],
    }]


def test_search_can_finish_before_a_real_client_function_call() -> None:
    parser = ResponsesProductionStreamParser(provider="deepseek", hosted_tool_type="web_search")
    search = SimpleNamespace(type="web_search_call", id="ws-1", status="completed", action=None)
    call = SimpleNamespace(
        type="function_call",
        id="fc-output",
        call_id="call-read",
        name="read_file",
        arguments='{"path":"README.md"}',
    )

    events = []
    events.extend(parser.push(SimpleNamespace(
        type="response.output_item.done", output_index=0, item=search
    )))
    events.extend(parser.push(SimpleNamespace(
        type="response.output_item.added", output_index=1, item=call
    )))
    events.extend(parser.push(SimpleNamespace(
        type="response.output_item.done", output_index=1, item=call
    )))
    events.extend(parser.push(SimpleNamespace(
        type="response.completed", response=_completed(search, call)
    )))

    assert [event.name for event in events if isinstance(event, ToolCallStart)] == ["read_file"]
    assert len([event for event in events if isinstance(event, ToolCallEnd)]) == 1
    assert parser.full_message()["tool_calls"][0]["id"] == "call-read"
    assert parser.full_message()[AURA_HOSTED_SEARCH_KEY]["search_count"] == 1


def test_native_search_without_client_function_completes_normally() -> None:
    parser = ResponsesProductionStreamParser(provider="openai", hosted_tool_type="web_search")
    search = SimpleNamespace(type="web_search_call", id="ws-1", status="completed", action=None)
    message = _message("grounded answer", [_citation()])
    events = parser.push(SimpleNamespace(
        type="response.completed", response=_completed(search, message)
    ))

    assert parser.status == "completed"
    assert parser.finish_reason == "stop"
    assert "tool_calls" not in parser.full_message()
    assert len([event for event in events if isinstance(event, Usage)]) == 1


def test_citations_are_safe_visible_and_deduplicated() -> None:
    parser = ResponsesProductionStreamParser(provider="openai", hosted_tool_type="web_search")
    message = _message(
        "grounded answer",
        [_citation(), _citation(), _citation("javascript:alert(1)")],
    )
    parser.push(SimpleNamespace(type="response.completed", response=_completed(message)))

    suffix_events = parser.emit_citation_suffix()
    assert [event.text for event in suffix_events if isinstance(event, ContentDelta)] == [
        "\n\nSources: [Example](<https://example.com/source>)"
    ]
    full = parser.full_message()
    assert "javascript:" not in full["content"]
    assert full[AURA_HOSTED_SEARCH_KEY]["citations"] == [
        {"title": "Example", "url": "https://example.com/source"}
    ]


def test_usage_is_emitted_once_even_if_completion_is_seen_twice() -> None:
    parser = ResponsesProductionStreamParser(provider="openai")
    event = SimpleNamespace(type="response.completed", response=_completed(_message()))
    events = parser.push(event) + parser.push(event)
    assert len([value for value in events if isinstance(value, Usage)]) == 1


def test_incomplete_and_failed_statuses_are_terminal() -> None:
    incomplete = ResponsesProductionStreamParser(provider="deepseek")
    incomplete.push(SimpleNamespace(
        type="response.incomplete",
        response=SimpleNamespace(
            id="r-incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=_usage(),
        ),
    ))
    assert incomplete.terminal is True
    assert "max_output_tokens" in incomplete.failure_message()

    failed = ResponsesProductionStreamParser(provider="openai")
    failed.push(SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(
            id="r-failed",
            error=SimpleNamespace(code="server_error", message="hosted search failed"),
        ),
    ))
    assert failed.terminal is True
    assert failed.failure_message() == "hosted search failed"


def test_cancel_marks_stream_settled_without_fabricating_search() -> None:
    parser = ResponsesProductionStreamParser(provider="openai", hosted_tool_type="web_search")
    parser.cancel()
    assert parser.settled is True
    assert AURA_HOSTED_SEARCH_KEY not in parser.full_message()
