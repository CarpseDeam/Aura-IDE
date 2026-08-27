"""Network-free provider-owned native web-search regressions."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

from aura.client import deepseek as ds
from aura.client.anthropic_stream import _to_anthropic_messages
from aura.client.chat_completions_transport import _strip_foreign_message_keys
from aura.client.deepseek_responses import project_responses_input
from aura.client.events import ApiError, Done, ToolCallStart, ToolResult, Usage
from aura.client.hosted_search import AURA_HOSTED_SEARCH_KEY, safe_web_url
from aura.conversation import ConversationManager, History
from aura.conversation.persistence import load_conversation, save_conversation
from aura.conversation.tools import ToolRegistry
from aura.conversation.tools.registry import TOOL_HANDLERS
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.providers.google_cloud.client import _google_tool_projection
from aura.providers.native_search import native_web_search_capability

LOCAL_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read one workspace file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def _usage():
    return SimpleNamespace(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        input_tokens_details=SimpleNamespace(cached_tokens=3),
    )


def _message(text: str, *, citation: bool = False):
    annotations = []
    if citation:
        annotations.append(SimpleNamespace(
            type="url_citation",
            url="https://example.com/aura",
            title="Aura source",
        ))
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(
            type="output_text",
            text=text,
            annotations=annotations,
        )],
    )


def _completed(*output):
    return SimpleNamespace(id="resp-1", output=list(output), usage=_usage())


def _client(provider: str, response_factory: Any) -> ds.DeepSeekClient:
    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = provider
    client._client = SimpleNamespace(responses=SimpleNamespace(create=response_factory))
    client._chat_protocol = "openai_chat"
    client._base_url = "https://example.invalid/v1"
    client._chat_base_url = "https://example.invalid/v1"
    client._api_key = "selected-provider-key"
    client._requires_reasoning_replay = provider in {"deepseek", "anthropic"}
    client._timeout = SimpleNamespace(connect=10.0, read=None)
    return client


def _tool_types(request: dict[str, Any]) -> list[str]:
    return [str(tool.get("type") or "") for tool in request.get("tools") or []]


def test_tool_registry_neither_exposes_nor_executes_web_search(tmp_path) -> None:
    registry = ToolRegistry(tmp_path)
    names = {tool["function"]["name"] for tool in registry.tool_defs()}

    assert "web_search" not in names
    assert "web_search" not in TOOL_HANDLERS
    result = registry.execute("web_search", {"question": "now"}, lambda _request: None)
    assert result.ok is False
    assert result.payload["error"] == "unknown tool: web_search"


def test_deepseek_key_state_cannot_change_another_providers_capability(
    tmp_path, monkeypatch,
) -> None:
    registry = ToolRegistry(tmp_path)
    before = registry.tool_defs()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present")
    after_present = registry.tool_defs()
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    after_absent = registry.tool_defs()

    capability = native_web_search_capability(
        "openai", "gpt-5.5", transport="responses"
    )
    assert capability is not None and capability.tool == {"type": "web_search"}
    assert before == after_present == after_absent


def test_deepseek_v4_request_combines_native_search_and_aura_functions_once() -> None:
    requests: list[dict[str, Any]] = []

    def create(**kwargs):
        requests.append(kwargs)
        return iter([SimpleNamespace(
            type="response.completed",
            response=_completed(_message("done")),
        )])

    events = list(_client("deepseek", create).stream(
        messages=[{"role": "user", "content": "inspect"}],
        tools=[LOCAL_TOOL],
        model="deepseek-v4-flash",
        thinking="high",
    ))

    assert len(requests) == 1
    assert _tool_types(requests[0]) == ["function", "web_search"]
    assert requests[0]["tools"][0]["name"] == "read_file"
    assert "tool_choice" not in requests[0]
    assert any(isinstance(event, Done) for event in events)


def test_openai_uses_selected_model_responses_search_and_only_openai_credentials(
    monkeypatch,
) -> None:
    resolved: list[str] = []
    requests: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, *, api_key, base_url, timeout, max_retries):
            assert api_key == "openai-secret"
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            requests.append(kwargs)
            return iter([SimpleNamespace(
                type="response.completed",
                response=_completed(_message("OpenAI answer")),
            )])

    def resolve(provider: str) -> str:
        resolved.append(provider)
        if provider != "openai":
            raise AssertionError("foreign credential access")
        return "openai-secret"

    monkeypatch.setattr(ds, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(ds, "resolve_api_key", resolve)
    client = ds.DeepSeekClient(provider="openai")
    list(client.stream(
        messages=[{"role": "user", "content": "current docs"}],
        tools=[LOCAL_TOOL],
        model="gpt-5.5",
        thinking="high",
    ))

    assert resolved == ["openai"]
    assert len(requests) == 1
    assert requests[0]["model"] == "gpt-5.5"
    assert _tool_types(requests[0]) == ["function", "web_search"]


def test_anthropic_request_combines_server_search_and_client_tools_without_events(
    monkeypatch,
) -> None:
    import aura.client.anthropic_stream as anthropic

    bodies: list[dict[str, Any]] = []
    server_events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": "srv-1",
                "name": "web_search",
                "input": {"query": "Aura"},
            },
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "web_search_tool_result",
                "tool_use_id": "srv-1",
                "content": [{"type": "web_search_result", "url": "https://example.com", "title": "Example"}],
            },
        },
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
    ]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, _method, _url, *, headers, json):
            bodies.append(json)
            return Response()

    monkeypatch.setattr(anthropic.httpx, "Client", Client)
    monkeypatch.setattr(anthropic, "_iter_anthropic_sse", lambda _response: iter(server_events))
    client = _client("anthropic", lambda **_kwargs: iter(()))
    client._chat_protocol = "anthropic_messages"
    events = list(client.stream(
        messages=[{"role": "user", "content": "current info"}],
        tools=[LOCAL_TOOL],
        model="claude-sonnet-4-6",
        thinking="off",
    ))

    assert [tool["name"] for tool in bodies[0]["tools"]] == ["read_file", "web_search"]
    assert bodies[0]["tools"][1]["type"] == "web_search_20260209"
    assert not any(isinstance(event, ToolCallStart) for event in events)
    done = next(event for event in events if isinstance(event, Done))
    assert "tool_calls" not in done.full_message
    assert done.full_message[AURA_HOSTED_SEARCH_KEY]["search_count"] == 1


def test_google_combines_search_and_functions_only_for_supported_model_transport() -> None:
    supported, capability = _google_tool_projection(
        [LOCAL_TOOL],
        model="gemini-3.5-flash",
        transport_supports_combined_tools=True,
    )
    unsupported_model, missing_model = _google_tool_projection(
        [LOCAL_TOOL],
        model="gemini-2.5-pro",
        transport_supports_combined_tools=True,
    )
    unsupported_transport, missing_transport = _google_tool_projection(
        [LOCAL_TOOL],
        model="gemini-3.5-flash",
        transport_supports_combined_tools=False,
    )

    assert supported[0] == {"google_search": {}}
    assert supported[1]["function_declarations"][0]["name"] == "read_file"
    assert capability is not None
    assert unsupported_model[0]["function_declarations"][0]["name"] == "read_file"
    assert unsupported_transport[0]["function_declarations"][0]["name"] == "read_file"
    assert missing_model is missing_transport is None


def test_openrouter_uses_current_server_tool_contract_without_online_or_deepseek() -> None:
    requests: list[dict[str, Any]] = []
    chunk = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(
                reasoning_content=None,
                content="grounded",
                tool_calls=None,
                annotations=[SimpleNamespace(
                    type="url_citation",
                    url_citation={"url": "https://example.com", "title": "Example"},
                )],
            ),
            finish_reason="stop",
        )],
    )

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return iter([chunk])

    client = _client("openrouter", lambda **_kwargs: iter(()))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    list(client.stream(
        messages=[{"role": "user", "content": "current info"}],
        tools=[LOCAL_TOOL],
        model="openai/gpt-oss-120b",
        thinking="off",
    ))

    assert requests[0]["model"] == "openai/gpt-oss-120b"
    assert not requests[0]["model"].endswith(":online")
    assert requests[0]["tools"][-1] == {"type": "openrouter:web_search"}
    assert "plugins" not in requests[0]


def test_external_cli_capabilities_receive_no_aura_search_proxy() -> None:
    for provider in ("codex", "claude_code"):
        capability = native_web_search_capability(
            provider,
            provider,
            transport="external_cli",
        )
        assert capability is None


def test_unsupported_specialized_models_omit_search_honestly() -> None:
    assert native_web_search_capability(
        "openai", "gpt-5-realtime", transport="responses"
    ) is None
    assert native_web_search_capability(
        "anthropic", "claude-3-haiku", transport="anthropic_messages"
    ) is None
    assert native_web_search_capability(
        "google_cloud",
        "gemini-3-pro-image-preview",
        transport="google_genai",
        transport_supports_combined_tools=True,
    ) is None


def test_search_then_client_call_uses_normal_round_and_retains_native_surface(
    tmp_path, monkeypatch,
) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    requests: list[dict[str, Any]] = []
    search = SimpleNamespace(type="web_search_call", id="ws-1", status="completed", action=None)
    function = SimpleNamespace(
        type="function_call",
        id="fc-1",
        call_id="call-read",
        name="read_file",
        arguments='{"path":"note.txt"}',
    )

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return iter([
                SimpleNamespace(type="response.output_item.done", output_index=0, item=search),
                SimpleNamespace(type="response.output_item.added", output_index=1, item=function),
                SimpleNamespace(type="response.output_item.done", output_index=1, item=function),
                SimpleNamespace(type="response.completed", response=_completed(search, function)),
            ])
        return iter([SimpleNamespace(
            type="response.completed", response=_completed(_message("finished"))
        )])

    client = _client("deepseek", create)
    history = History()
    history.append_user_text("research, then inspect note.txt")
    manager = ConversationManager(history, ToolRegistry(tmp_path))
    events: list[Any] = []
    from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, client.stream)
    try:
        manager.send(
            on_event=events.append,
            approval_cb=lambda _request: None,
            cancel_event=threading.Event(),
            model="deepseek-v4-flash",
            thinking="high",
        )
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)

    assert len(requests) == 2
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[0]["tools"][-1] == {"type": "web_search"}
    assert [event.name for event in events if isinstance(event, ToolCallStart)] == ["read_file"]
    assert [event.name for event in events if isinstance(event, ToolResult)] == ["read_file"]
    assert any(item.get("type") == "web_search_call" for item in requests[1]["input"])


def test_native_search_only_finishes_without_an_aura_tool_round() -> None:
    requests: list[dict[str, Any]] = []
    search = SimpleNamespace(type="web_search_call", id="ws-1", status="completed", action=None)

    def create(**kwargs):
        requests.append(kwargs)
        return iter([SimpleNamespace(
            type="response.completed",
            response=_completed(search, _message("grounded", citation=True)),
        )])

    events = list(_client("openai", create).stream(
        messages=[{"role": "user", "content": "current info"}],
        tools=[LOCAL_TOOL],
        model="gpt-5.5",
        thinking="high",
    ))
    done = next(event for event in events if isinstance(event, Done))
    assert len(requests) == 1
    assert "tool_calls" not in done.full_message
    assert "https://example.com/aura" in done.full_message["content"]


def test_hosted_metadata_persists_but_never_leaks_to_another_provider(tmp_path) -> None:
    metadata = {
        "provider": "openai",
        "tool_type": "web_search",
        "citations": [{"title": "Source", "url": "https://example.com"}],
        "wire_items": [{"type": "web_search_call", "id": "ws-1", "status": "completed"}],
    }
    history = History()
    history.append_user_text("question")
    history.append_assistant({
        "role": "assistant",
        "content": "answer\n\nSources: [Source](<https://example.com>)",
        AURA_HOSTED_SEARCH_KEY: metadata,
    })
    path = save_conversation(history, tmp_path, model="gpt-5.5", thinking="high", provider="openai")
    loaded = load_conversation(path)

    assert loaded.history.messages[-1][AURA_HOSTED_SEARCH_KEY] == metadata
    _instructions, deepseek_items = project_responses_input(
        loaded.history.for_api(), provider="deepseek"
    )
    assert not any(item.get("type") == "web_search_call" for item in deepseek_items)
    _system, anthropic_messages = _to_anthropic_messages(
        loaded.history.for_api(), provider="anthropic"
    )
    assert AURA_HOSTED_SEARCH_KEY not in json.dumps(anthropic_messages)
    assert AURA_HOSTED_SEARCH_KEY not in _strip_foreign_message_keys(
        loaded.history.for_api()
    )[-1]


def test_cancellation_and_hosted_errors_never_fall_back_to_chat() -> None:
    chat_calls: list[dict[str, Any]] = []

    class Chat:
        def create(self, **kwargs):
            chat_calls.append(kwargs)
            return iter(())

    def create(**_kwargs):
        raise RuntimeError("hosted search failed")

    client = _client("openai", create)
    client._client.chat = SimpleNamespace(completions=Chat())
    failed = list(client.stream(
        messages=[{"role": "user", "content": "current info"}],
        tools=[LOCAL_TOOL],
        model="gpt-5.5",
        thinking="high",
    ))
    assert len([event for event in failed if isinstance(event, ApiError)]) == 1
    assert chat_calls == []

    cancel = threading.Event()
    cancel.set()
    cancelled = list(_client("openai", lambda **_kwargs: iter(())).stream(
        messages=[{"role": "user", "content": "current info"}],
        tools=[LOCAL_TOOL],
        model="gpt-5.5",
        thinking="high",
        cancel_event=cancel,
    ))
    assert any(isinstance(event, Done) and event.finish_reason == "cancelled" for event in cancelled)


def test_usage_is_attributed_once_to_the_selected_request() -> None:
    search = SimpleNamespace(type="web_search_call", id="ws-1", status="completed", action=None)
    events = list(_client(
        "openai",
        lambda **_kwargs: iter([SimpleNamespace(
            type="response.completed", response=_completed(search, _message("done"))
        )]),
    ).stream(
        messages=[{"role": "user", "content": "current info"}],
        tools=[LOCAL_TOOL],
        model="gpt-5.5",
        thinking="high",
    ))
    usage = [event for event in events if isinstance(event, Usage)]
    assert len(usage) == 1
    assert usage[0].prompt_tokens == 11


def test_non_search_chat_turn_keeps_exact_client_tool_surface() -> None:
    requests: list[dict[str, Any]] = []
    chunk = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(reasoning_content=None, content="done", tool_calls=None),
            finish_reason="stop",
        )],
    )

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return iter([chunk])

    client = _client("compatible_provider", lambda **_kwargs: iter(()))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    list(client.stream(
        messages=[{"role": "user", "content": "inspect"}],
        tools=[LOCAL_TOOL],
        model="compatible-model",
        thinking="off",
    ))
    assert requests[0]["tools"] == [LOCAL_TOOL]


def test_read_only_local_policy_is_unchanged_and_search_links_reject_unsafe_urls(tmp_path) -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    registry = ToolRegistry(tmp_path, read_only=True)
    names = {tool["function"]["name"] for tool in registry.tool_defs()}
    assert names == {
        "read_file", "grep_search", "glob", "git_status", "git_diff",
        "git_log", "git_show", "git_log_file", "git_branch_list",
        "git_stash_list", "git_stash_show",
    }
    assert "web_search" not in names
    assert safe_web_url("javascript:alert(1)") == ""
    assert safe_web_url("file:///C:/secret") == ""
    rendered = _render_markdown_with_code(
        "[safe](https://example.com) [unsafe](javascript:alert(1))"
    )
    assert 'href="https://example.com"' in rendered
    assert "javascript:" not in rendered
    assert app is not None
