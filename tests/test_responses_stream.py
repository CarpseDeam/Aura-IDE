"""Focused tests for Responses API tool translation and stream normalization.

Covers the exact DeepSeek Responses API tool schema, ordinary function
conversion, web_search_call streaming events, final response parsing with
citations, completed/incomplete/failed responses, and usage parsing.
"""

from __future__ import annotations

from types import SimpleNamespace

from aura.client.events import (
    ContentDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from aura.client.responses_common import translate_to_responses_tools
from aura.client.responses_web_search import (
    RESPONSES_WEB_SEARCH_TOOL,
    ResponsesStreamParser,
    build_native_web_search_request,
)

# ---------------------------------------------------------------------------
# Tool schema translation
# ---------------------------------------------------------------------------


def test_web_search_capability_translates_to_native_tool():
    """The provider-neutral web_search capability maps to the native built-in."""
    translated = translate_to_responses_tools([{"type": "web_search"}])
    assert translated == [{"type": "web_search"}]


def test_web_search_capability_uses_stable_type():
    """Only the stable 'web_search' type is ever emitted."""
    assert RESPONSES_WEB_SEARCH_TOOL == {"type": "web_search"}


def test_ordinary_function_tool_converts_to_flat_responses_shape():
    """Aura function tools flatten to the Responses API function shape."""
    aura_def = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
    translated = translate_to_responses_tools([aura_def])
    assert translated == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    assert "function" not in translated[0]


def test_translate_tools_none_and_empty():
    assert translate_to_responses_tools(None) is None
    assert translate_to_responses_tools([]) is None


def test_translate_mixed_tool_list():
    translated = translate_to_responses_tools(
        [
            {"type": "web_search"},
            {
                "type": "function",
                "function": {"name": "git_status", "parameters": {}},
            },
        ]
    )
    assert translated == [
        {"type": "web_search"},
        {"type": "function", "name": "git_status", "parameters": {}},
    ]


# ---------------------------------------------------------------------------
# Native request construction
# ---------------------------------------------------------------------------


def test_build_native_web_search_request_exact_shape():
    request = build_native_web_search_request(
        "Are there any World Cup matches today?",
        context="User is in London.",
        model="deepseek-v4-flash",
    )
    assert request == {
        "model": "deepseek-v4-flash",
        "input": [
            {
                "role": "user",
                "content": (
                    "Are there any World Cup matches today?\n\n"
                    "Relevant context:\nUser is in London."
                ),
            }
        ],
        "tools": [{"type": "web_search"}],
        "tool_choice": {"type": "web_search"},
        "stream": True,
    }


def test_build_native_web_search_request_without_context():
    request = build_native_web_search_request("What is the latest Aura version?")
    assert request["input"] == [
        {"role": "user", "content": "What is the latest Aura version?"}
    ]
    assert request["tools"] == [{"type": "web_search"}]
    # The built-in is named explicitly so the model cannot skip searching.
    assert request["tool_choice"] == {"type": "web_search"}
    assert request["stream"] is True


# ---------------------------------------------------------------------------
# Streaming event normalization
# ---------------------------------------------------------------------------


def _search_call_event(kind: str, item_id: str = "ws_1") -> SimpleNamespace:
    return SimpleNamespace(type=kind, item_id=item_id, output_index=0)


def test_web_search_call_streaming_events():
    parser = ResponsesStreamParser()
    events = []
    for kind in (
        "response.web_search_call.in_progress",
        "response.web_search_call.searching",
        "response.web_search_call.completed",
    ):
        events.extend(parser.push(_search_call_event(kind)))

    # status transitions are tracked on one item
    assert len(parser.web_search_calls) == 1
    assert parser.web_search_calls[0]["item_id"] == "ws_1"
    assert parser.web_search_calls[0]["status"] == "completed"
    assert events == []


def test_web_search_call_via_output_item_added_done():
    parser = ResponsesStreamParser()
    parser.push(
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(
                type="web_search_call", id="ws_2", status="in_progress"
            ),
        )
    )
    parser.push(
        SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=SimpleNamespace(
                type="web_search_call",
                id="ws_2",
                status="completed",
                search_recipient="bing",
            ),
        )
    )
    assert len(parser.web_search_calls) == 1
    assert parser.web_search_calls[0]["status"] == "completed"
    assert parser.web_search_calls[0]["search_recipient"] == "bing"


def test_output_text_delta_and_done():
    parser = ResponsesStreamParser()
    events = parser.push(
        SimpleNamespace(type="response.output_text.delta", delta="Hel", item_id="m1")
    )
    events += parser.push(
        SimpleNamespace(type="response.output_text.delta", delta="lo", item_id="m1")
    )
    events += parser.push(
        SimpleNamespace(
            type="response.output_text.done", text="Hello world", item_id="m1"
        )
    )
    assert [e.text for e in events if isinstance(e, ContentDelta)] == ["Hel", "lo"]
    assert parser.text == "Hello world"
    assert parser.status == "in_progress"


def test_function_call_arguments_delta_done():
    parser = ResponsesStreamParser()
    # ToolCallStart is emitted by the item.added push, so collect from there.
    events = parser.push(
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(
                type="function_call",
                id="fc_1",
                name="read_file",
                arguments="",
                status="in_progress",
            ),
        )
    )
    events += parser.push(
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            delta='{"path":',
            output_index=0,
        )
    )
    events += parser.push(
        SimpleNamespace(
            type="response.function_call_arguments.done",
            arguments='{"path": "aura/config.py"}',
            output_index=0,
        )
    )
    events += parser.push(
        SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=SimpleNamespace(
                type="function_call",
                id="fc_1",
                name="read_file",
                arguments='{"path": "aura/config.py"}',
                status="completed",
            ),
        )
    )
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    deltas = [e for e in events if isinstance(e, ToolCallArgsDelta)]
    ends = [e for e in events if isinstance(e, ToolCallEnd)]
    assert starts and starts[0].name == "read_file"
    assert deltas and deltas[0].args_chunk == '{"path":'
    assert ends and ends[0].index == 0
    assert parser._pending_function_calls[0]["arguments"] == '{"path": "aura/config.py"}'


# ---------------------------------------------------------------------------
# Final response parsing and citations
# ---------------------------------------------------------------------------


def _completed_response_event(usage=None, output=None):
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            id="resp_123",
            status="completed",
            usage=usage or _fake_usage(),
            output=output or [_message_item("Answer text")],
        ),
    )


def _fake_usage(
    input_tokens=120,
    output_tokens=45,
    total_tokens=165,
    cached_tokens=30,
):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )


def _message_item(text: str, annotations=None):
    return SimpleNamespace(
        type="message",
        id="msg_1",
        status="completed",
        role="assistant",
        content=[
            SimpleNamespace(
                type="output_text",
                text=text,
                annotations=annotations or [],
            )
        ],
    )


def test_completed_response_parsing_and_citations():
    parser = ResponsesStreamParser()
    parser.push(
        SimpleNamespace(type="response.output_text.delta", delta="Ans", item_id="m1")
    )
    events = parser.push(
        _completed_response_event(
            output=[
                _message_item(
                    "Answer text",
                    annotations=[
                        SimpleNamespace(
                            type="url_citation",
                            start_index=0,
                            end_index=8,
                            url="https://example.com/news",
                            title="Example News",
                        ),
                        SimpleNamespace(
                            type="url_citation",
                            start_index=9,
                            end_index=20,
                            url="https://example.com/scores",
                            title="Example Scores",
                        ),
                    ],
                ),
                SimpleNamespace(
                    type="web_search_call",
                    id="ws_9",
                    status="completed",
                    search_recipient="bing",
                ),
            ]
        )
    )

    assert parser.status == "completed"
    assert parser.terminal is True
    assert parser.text == "Answer text"
    assert parser.response_id == "resp_123"
    assert parser.sources == [
        {"title": "Example News", "url": "https://example.com/news"},
        {"title": "Example Scores", "url": "https://example.com/scores"},
    ]
    assert any(
        c.get("status") == "completed" and c.get("item_id") == "ws_9"
        for c in parser.web_search_calls
    )
    usage_events = [e for e in events if isinstance(e, Usage)]
    assert len(usage_events) == 1
    assert usage_events[0].prompt_tokens == 120
    assert usage_events[0].completion_tokens == 45
    assert usage_events[0].cache_hit_tokens == 30
    assert usage_events[0].cache_miss_tokens == 90

    final = parser.finish()
    assert final["status"] == "completed"
    assert final["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "cache_hit_tokens": 30,
        "cache_miss_tokens": 90,
        "total_tokens": 165,
    }
    assert final["sources"][0]["url"] == "https://example.com/news"


def test_sources_deduplicated():
    parser = ResponsesStreamParser()
    citation = SimpleNamespace(
        type="url_citation",
        start_index=0,
        end_index=5,
        url="https://example.com/same",
        title="Same",
    )
    # Same citation seen via output_item.done and response.completed output.
    parser.push(
        SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=_message_item("text", annotations=[citation]),
        )
    )
    parser.push(_completed_response_event(output=[_message_item("text", [citation])]))
    assert len(parser.sources) == 1


def test_incomplete_response():
    parser = ResponsesStreamParser()
    parser.push(
        SimpleNamespace(
            type="response.incomplete",
            response=SimpleNamespace(
                id="resp_2",
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
                usage=_fake_usage(),
                output=[],
            ),
        )
    )
    assert parser.status == "incomplete"
    assert parser.terminal is True
    assert parser.incomplete_reason == "max_output_tokens"
    assert parser.finish()["incomplete_reason"] == "max_output_tokens"


def test_failed_response():
    parser = ResponsesStreamParser()
    parser.push(
        SimpleNamespace(
            type="response.failed",
            response=SimpleNamespace(
                id="resp_3",
                status="failed",
                error={
                    "code": "server_error",
                    "message": "upstream search failed",
                    "status_code": 503,
                },
            ),
        )
    )
    assert parser.status == "failed"
    assert parser.terminal is True
    assert parser.error_code == "server_error"
    assert "upstream search failed" in (parser.error or "")


def test_error_sse_event():
    parser = ResponsesStreamParser()
    parser.push(
        SimpleNamespace(type="error", code="rate_limit_exceeded", message="slow down")
    )
    assert parser.status == "failed"
    assert parser.error_code == "rate_limit_exceeded"
    assert parser.error == "slow down"


def test_usage_parsing_without_details():
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_tokens_details=None,
    )
    parser = ResponsesStreamParser()
    events = parser.push(
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                id="r", status="completed", usage=usage, output=[]
            ),
        )
    )
    usage_events = [e for e in events if isinstance(e, Usage)]
    assert usage_events[0].cache_hit_tokens == 0
    assert usage_events[0].cache_miss_tokens == 10


def test_finish_is_neutral_payload():
    """finish() never leaks raw response event types into the payload."""
    parser = ResponsesStreamParser()
    parser.push(_completed_response_event())
    final = parser.finish()
    assert "response.completed" not in final
    assert "web_search_call" not in [k for k in final.keys()]
    assert set(final.keys()) == {
        "status",
        "text",
        "sources",
        "web_search_calls",
        "usage",
        "incomplete_reason",
        "error",
        "error_code",
        "response_id",
    }


def test_cancel_marks_status():
    parser = ResponsesStreamParser()
    parser.cancel()
    assert parser.status == "cancelled"
    assert parser.finish()["status"] == "cancelled"
