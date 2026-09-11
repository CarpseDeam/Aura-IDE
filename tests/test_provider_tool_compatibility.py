"""Synthetic provider streams must survive strict tool preflight and replay."""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk

from aura.client.chat_completions_transport import stream_chat_completions
from aura.client.events import ApiError, ContentDelta, Done, ReasoningDelta, ToolCallStart, ToolResult
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools import fs_read
from aura.conversation.tools.registry import ToolRegistry


def _chunk(**delta):
    return ChatCompletionChunk.model_validate({
        "id": "completion", "created": 0, "model": "test", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    })


def _stream(chunks, messages=None, provider="openrouter", thinking="off"):
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return iter(chunks)

    events = list(stream_chat_completions(
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        provider=provider, chat_protocol="openai_chat", base_url="https://example.invalid/v1",
        timeout=SimpleNamespace(connect=10, read=None), messages=messages or [],
        tools=None, model="test", thinking=thinking,
    ))
    return events, requests[0]


def _dsml(calls, token="｜DSML｜", wrapper="function_calls"):
    # DeepSeek's official encoding_dsv32.py: strings literal, everything else JSON.
    body = ""
    for name, args in calls:
        body += f'<{token}invoke name="{name}">\n'
        for key, value in args.items():
            flag = "true" if isinstance(value, str) else "false"
            encoded = value if isinstance(value, str) else json.dumps(value)
            body += f'<{token}parameter name="{key}" string="{flag}">{encoded}</{token}parameter>\n'
        body += f'</{token}invoke>\n'
    return f'<{token}{wrapper}>\n{body}</{token}{wrapper}>'


def _native(calls, size):
    chunks = []
    for index, (name, args) in enumerate(calls):
        raw = args if isinstance(args, str) else json.dumps(args)
        # Stream metadata arrives once; later deltas carry only arguments.
        chunks.append(_chunk(tool_calls=[{
            "index": index, "id": f"call_{index}", "type": "function",
            "function": {"name": name, "arguments": ""},
        }]))
        for start in range(0, len(raw), size):
            chunks.append(_chunk(tool_calls=[{
                "index": index, "function": {"arguments": raw[start:start + size]},
            }]))
    return chunks


def _execute(root, calls, *, read_only=False, history=None):
    history = history or History()
    tools = ToolRegistry(workspace_root=root, read_only=read_only)
    runner = ToolRoundRunner(history=history, tools=tools, tool_runner=ToolRunner(history, root))
    events = []
    runner.run(tool_calls=calls, on_event=events.append, approval_cb=lambda _: pytest.fail("Unexpected approval"),
               cancel_event=threading.Event(), cleanup_cancelled=lambda _: None)
    return [event for event in events if isinstance(event, ToolResult)]


@pytest.mark.parametrize("size", [1, 17, 10000])
@pytest.mark.parametrize("dialect", [("｜DSML｜", "function_calls"), ("｜｜DSML｜｜", "tool_calls")])
def test_native_and_dsml_reads_and_searches_are_equivalent(tmp_path, size, dialect):
    (tmp_path / "a.md").write_bytes(b"# A\nneedle here\nlast\n")
    (tmp_path / "b.md").write_bytes(b"# B\nNEEDLE too\n")
    calls = [
        ("read_file", {"paths": ["a.md", "b.md"]}),
        ("read_file", {"path": "a.md", "offset": 2, "limit": 1}),
        ("grep_search", {"pattern": "needle", "path": ".", "case_sensitive": False,
                         "max_results": 3, "regex_mode": False}),
    ]
    markup = "Before\n" + _dsml(calls, *dialect) + "\nAfter"
    dsml, _ = _stream([_chunk(content=markup[i:i + size]) for i in range(0, len(markup), size)])
    native, _ = _stream(_native(calls, size))
    assert "".join(e.text for e in dsml if isinstance(e, ContentDelta)) == "Before\n\nAfter"
    results = []
    for events in (dsml, native):
        assert not any(isinstance(e, ApiError) for e in events)
        parsed = next(e.full_message["tool_calls"] for e in events if isinstance(e, Done))
        assert [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in parsed] == calls
        executed = _execute(tmp_path, parsed)
        assert len(executed) == 3
        assert all(r.ok for r in executed), [r.result for r in executed]
        results.append([json.loads(r.result) for r in executed])
        # ripgrep may report files in either order when searching in parallel.
        results[-1][2]["matches"].sort(key=lambda match: match["path"])
    assert results[0] == results[1]
    assert results[0][1]["content"] == "needle here\n"


@pytest.mark.parametrize("bad", ['["a.md",]', 'NaN', '<broken>', 'true false'])
def test_invalid_dsml_json_fails_without_a_completed_call(bad):
    markup = _dsml([("read_file", {"paths": ["a.md"]})]).replace('["a.md"]', bad)
    events, _ = _stream([_chunk(content=c) for c in markup])
    assert any(isinstance(e, ApiError) for e in events)
    assert not any(isinstance(e, (ToolCallStart, Done)) for e in events)


def test_literal_strings_and_malformed_native_arguments_fail_strict_preflight(tmp_path):
    (tmp_path / "a.md").write_text("real contents", encoding="utf-8")
    for markup in (
        _dsml([("read_file", {"paths": '["a.md"]'})]),
        _dsml([("read_file", {"paths": '["a.md"]'})]).replace(' string="true"', ''),
    ):
        events, _ = _stream([_chunk(content=markup)])
        calls = events[-1].full_message["tool_calls"]
        assert json.loads(calls[0]["function"]["arguments"])["paths"] == '["a.md"]'
        result = _execute(tmp_path, calls)[0]
        assert not result.ok
        assert result.extras["failure_class"] == "tool_call_schema_violation"
    events, _ = _stream(_native([("read_file", '{"paths": [')], 1))
    assert not _execute(tmp_path, events[-1].full_message["tool_calls"])[0].ok


def test_openrouter_reasoning_reaches_history_and_tool_continuation(tmp_path):
    (tmp_path / "a.md").write_text("# Evidence", encoding="utf-8")
    details = [
        {"type": "reasoning.text", "text": "Read ", "index": 4, "id": "r", "signature": None},
        {"type": "reasoning.text", "text": "the file.", "index": 4, "id": "r", "signature": "signed"},
        {"type": "reasoning.encrypted", "data": "opaque", "index": 1, "format": "google-gemini-v1"},
    ]
    events, _ = _stream([
        _chunk(reasoning="Read ", reasoning_content="Read "),
        _chunk(reasoning_details=[details[0]]),
        _chunk(reasoning="the file.", reasoning_content="the file.", reasoning_details=details[1:]),
        *_native([("read_file", {"path": "a.md"})], 3),
    ])
    assert "".join(e.text for e in events if isinstance(e, ReasoningDelta)) == "Read the file."
    message = events[-1].full_message
    assert message["reasoning"] == "Read the file."
    assert message["reasoning_details"] == details
    history = History()
    history.append_assistant(message)
    assert _execute(tmp_path, message["tool_calls"], history=history)[0].ok
    _, request = _stream([_chunk(content="Read it.")], history.for_api())
    assistant = request["messages"][0]
    assert assistant["reasoning"] == "Read the file."
    assert assistant["reasoning_details"] == details
    assert "reasoning_content" not in assistant
    assert request["messages"][1]["role"] == "tool"
    assert history.messages[0]["reasoning_content"] == "Read the file."
    _, other = _stream([], history.for_api(), provider="deepseek")
    assert "reasoning_details" not in other["messages"][0]
    assert other["messages"][0]["reasoning_content"] == "Read the file."


def test_details_only_reasoning_and_late_alias_are_displayed_once():
    events, _ = _stream([
        _chunk(reasoning_details=[{"type": "reasoning.summary", "summary": "Inspect "}]),
        _chunk(reasoning_details=[{"type": "reasoning.summary", "summary": "files."}]),
        _chunk(reasoning="Inspect files."),
    ])
    assert "".join(e.text for e in events if isinstance(e, ReasoningDelta)) == "Inspect files."
    assert events[-1].full_message["reasoning_content"] == "Inspect files."


def test_transport_uses_selected_models_reasoning_settings(monkeypatch):
    from aura.providers.base import ModelInfo
    from aura.providers.registry import provider_registry

    model = ModelInfo("test", "Test", 0, 0, 0, reasoning={"supported_efforts": ["xhigh", "high"]})
    monkeypatch.setitem(provider_registry.get("openrouter").models, "test", model)
    _, request = _stream([], thinking="max")
    assert request["extra_body"] == {"reasoning": {"enabled": True, "effort": "xhigh"}}
    assert "reasoning_effort" not in request
    _, request = _stream([], thinking="off")
    assert request["extra_body"] == {"reasoning": {"enabled": False}}


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("cut", [6, 8])
def test_truncated_read_continuation_uses_exposed_tool(tmp_path, monkeypatch, read_only, cut):
    monkeypatch.setattr(fs_read, "MAX_READ_BYTES", cut)
    (tmp_path / "large.md").write_bytes(b"first\nsecond\nlast\n")
    tools = ToolRegistry(workspace_root=tmp_path, read_only=read_only)
    exposed = {t["function"]["name"] for t in tools.tool_defs()}
    assert "read_file" in exposed and "read_file_range" not in exposed
    initial = tools.execute("read_file", {"path": "large.md"}, approval_cb=None).payload
    assert initial["truncated"]
    assert "read_file_range" not in initial["content"]
    match = re.search(r'read_file\(path, offset=(\d+), limit=(\d+)\)', initial["content"])
    assert match
    args = {"path": "large.md", "offset": int(match[1]), "limit": int(match[2])}
    result = tools.execute("read_file", args, approval_cb=None)
    assert result.ok
    assert result.payload["content"] == "second\nlast\n"
