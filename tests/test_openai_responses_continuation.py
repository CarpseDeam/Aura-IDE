"""Offline contracts for OpenAI Responses reasoning continuation and gating.

Every provider interaction here is a captured/fake Responses event stream. No
OpenAI, DeepSeek, Anthropic, Gemini, OpenRouter, CLI, or web request is made.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aura.client import deepseek as ds
from aura.client.anthropic_stream import _to_anthropic_messages
from aura.client.chat_completions_transport import _strip_foreign_message_keys
from aura.client.hosted_search import AURA_HOSTED_SEARCH_KEY
from aura.client.responses_continuation import AURA_PROVIDER_REASONING_KEY
from aura.client.responses_request import (
    build_responses_request,
    project_responses_input,
)
from aura.client.responses_stream import ResponsesProductionStreamParser
from aura.conversation import ConversationManager, History
from aura.conversation.persistence import load_conversation, save_conversation
from aura.conversation.tools import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from aura.providers.native_search import native_web_search_capability

REASONING_ID = "rs-openai-1"
ENCRYPTED = "gAAAAAB-opaque-provider-state"


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=40,
        output_tokens=9,
        total_tokens=49,
        input_tokens_details=SimpleNamespace(cached_tokens=8),
    )


def _reasoning_item(
    item_id: str = REASONING_ID,
    *,
    summary: str = "Checking the file first.",
    encrypted: str = ENCRYPTED,
) -> SimpleNamespace:
    return SimpleNamespace(
        type="reasoning",
        id=item_id,
        summary=[SimpleNamespace(type="summary_text", text=summary)],
        encrypted_content=encrypted,
        status="completed",
    )


def _function_item(call_id: str, path: str, item_id: str = "fc-1") -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        id=item_id,
        call_id=call_id,
        name="read_file",
        arguments=json.dumps({"path": path}),
    )


def _message_item(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text, annotations=[])],
    )


def _completed(*output: Any) -> SimpleNamespace:
    return SimpleNamespace(id="resp-openai", output=list(output), usage=_usage())


def _stream_events(*output: Any) -> list[SimpleNamespace]:
    """Build a realistic streamed Responses turn for the given output items."""
    events: list[SimpleNamespace] = []
    for index, item in enumerate(output):
        events.append(
            SimpleNamespace(
                type="response.output_item.added", output_index=index, item=item
            )
        )
        events.append(
            SimpleNamespace(
                type="response.output_item.done", output_index=index, item=item
            )
        )
    events.append(
        SimpleNamespace(type="response.completed", response=_completed(*output))
    )
    return events


def _client(provider: str, factory: Any) -> ds.DeepSeekClient:
    client = ds.DeepSeekClient.__new__(ds.DeepSeekClient)
    client._provider = provider
    client._client = SimpleNamespace(responses=SimpleNamespace(create=factory))
    client._chat_protocol = "openai_chat"
    client._base_url = "https://example.invalid/v1"
    client._chat_base_url = "https://example.invalid/v1"
    client._api_key = "selected-provider-key"
    client._requires_reasoning_replay = provider == "deepseek"
    client._timeout = SimpleNamespace(connect=10.0, read=None)
    return client


def _parse(provider: str, *output: Any) -> ResponsesProductionStreamParser:
    parser = ResponsesProductionStreamParser(
        provider=provider, hosted_tool_type="web_search"
    )
    for event in _stream_events(*output):
        parser.push(event)
    return parser


def _wire_kinds(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("type") or item.get("role") or "") for item in items]


# ── 1. the complete provider reasoning item is stored ───────────────────────


def test_openai_reasoning_plus_function_call_stores_the_complete_item() -> None:
    parser = _parse(
        "openai", _reasoning_item(), _function_item("call-read-1", "note.txt")
    )
    message = parser.full_message()
    stored = message[AURA_PROVIDER_REASONING_KEY]

    assert stored["provider"] == "openai"
    assert [entry["kind"] for entry in stored["entries"]] == [
        "reasoning",
        "function_call",
    ]
    assert stored["entries"][0]["item"] == {
        "id": REASONING_ID,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "Checking the file first."}],
        "encrypted_content": ENCRYPTED,
        "status": "completed",
    }
    assert stored["entries"][1]["call_id"] == "call-read-1"
    # The metadata is JSON-safe, so persistence keeps it verbatim.
    assert json.loads(json.dumps(stored)) == stored
    # Only the provider's visible summary is exposed as reasoning; the
    # encrypted continuation state never becomes chat content.
    assert message["reasoning_content"] == "Checking the file first."
    assert ENCRYPTED not in message["content"]
    assert ENCRYPTED not in message["reasoning_content"]


def test_openai_reasoning_without_a_function_call_stores_no_continuation_state() -> None:
    parser = _parse("openai", _reasoning_item(), _message_item("plain answer"))

    assert AURA_PROVIDER_REASONING_KEY not in parser.full_message()
    assert parser.full_message()["reasoning_content"] == "Checking the file first."


# ── 2. the next Aura tool round replays it in the required wire order ───────


def test_next_openai_round_sends_reasoning_then_call_then_output(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "note.txt").write_text("contents", encoding="utf-8")
    requests: list[dict[str, Any]] = []

    first = _stream_events(
        _reasoning_item(), _function_item("call-read-1", "note.txt")
    )
    second = _stream_events(_message_item("validated"))

    class Responses:
        def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            return iter(first if len(requests) == 1 else second)

    client = _client("openai", None)
    client._client = SimpleNamespace(responses=Responses())
    registry = ModelStreamRegistry()
    registry.register(PRODUCTION_STREAM_HOOK, client.stream)
    monkeypatch.setattr("aura.conversation.manager.model_streams", registry)

    history = History()
    history.set_system("system")
    history.append_user_text("Read note.txt and validate it.")
    manager = ConversationManager(history, ToolRegistry(tmp_path))
    manager.send(
        on_event=lambda _event: None,
        approval_cb=lambda _request: None,
        cancel_event=threading.Event(),
        model="gpt-5.5",
        thinking="high",
    )

    assert len(requests) == 2
    second_input = requests[1]["input"]
    assert _wire_kinds(second_input) == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert second_input[1]["id"] == REASONING_ID
    assert second_input[1]["encrypted_content"] == ENCRYPTED
    assert second_input[2] == {
        "type": "function_call",
        "call_id": "call-read-1",
        "name": "read_file",
        "arguments": json.dumps({"path": "note.txt"}),
    }
    assert second_input[3]["call_id"] == "call-read-1"
    # Aura's visible reasoning text is never reconstructed onto the wire.
    assert "reasoning_content" not in json.dumps(second_input)


# ── 3. multiple calls keep their original reasoning/call ordering ───────────


def test_multiple_function_calls_keep_original_reasoning_and_call_order() -> None:
    parser = _parse(
        "openai",
        _reasoning_item("rs-a", summary="first", encrypted="enc-a"),
        _function_item("call-a", "a.txt", item_id="fc-a"),
        _reasoning_item("rs-b", summary="second", encrypted="enc-b"),
        _function_item("call-b", "b.txt", item_id="fc-b"),
    )
    assistant = parser.full_message()
    _instructions, items = project_responses_input(
        [
            {"role": "user", "content": "read both"},
            assistant,
            {"role": "tool", "tool_call_id": "call-a", "content": "A"},
            {"role": "tool", "tool_call_id": "call-b", "content": "B"},
        ],
        provider="openai",
    )

    assert _wire_kinds(items) == [
        "user",
        "reasoning",
        "function_call",
        "reasoning",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert [items[1]["id"], items[3]["id"]] == ["rs-a", "rs-b"]
    assert [items[2]["call_id"], items[4]["call_id"]] == ["call-a", "call-b"]
    assert [items[1]["encrypted_content"], items[3]["encrypted_content"]] == [
        "enc-a",
        "enc-b",
    ]


# ── 4. persistence keeps the continuation state ────────────────────────────


def test_saving_and_reloading_preserves_openai_continuation_state(tmp_path) -> None:
    parser = _parse(
        "openai", _reasoning_item(), _function_item("call-read-1", "note.txt")
    )
    history = History()
    history.append_user_text("read note.txt")
    history.append_assistant(parser.full_message())
    history.append_tool_result("call-read-1", "contents")

    path = save_conversation(
        history, tmp_path, model="gpt-5.5", thinking="high", provider="openai"
    )
    loaded = load_conversation(path)

    assert (
        loaded.history.messages[1][AURA_PROVIDER_REASONING_KEY]
        == parser.full_message()[AURA_PROVIDER_REASONING_KEY]
    )
    _instructions, items = project_responses_input(
        loaded.history.for_api(), provider="openai"
    )
    assert _wire_kinds(items) == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert items[1]["encrypted_content"] == ENCRYPTED


# ── 5/6. the state never leaks to another provider ─────────────────────────


def test_openai_reasoning_state_never_reaches_another_provider() -> None:
    parser = _parse(
        "openai", _reasoning_item(), _function_item("call-read-1", "note.txt")
    )
    messages = [
        {"role": "user", "content": "read note.txt"},
        parser.full_message(),
        {"role": "tool", "tool_call_id": "call-read-1", "content": "contents"},
    ]

    for foreign in ("deepseek", "openrouter", "anthropic", "google_cloud"):
        _instructions, items = project_responses_input(messages, provider=foreign)
        assert "reasoning" not in _wire_kinds(items)
        assert ENCRYPTED not in json.dumps(items)

    _system, anthropic_messages = _to_anthropic_messages(
        messages, provider="anthropic"
    )
    assert AURA_PROVIDER_REASONING_KEY not in json.dumps(anthropic_messages)
    assert ENCRYPTED not in json.dumps(anthropic_messages)

    stripped = _strip_foreign_message_keys(messages)
    assert AURA_PROVIDER_REASONING_KEY not in stripped[1]
    assert AURA_HOSTED_SEARCH_KEY not in stripped[1]


def test_deepseek_responses_still_omits_prior_reasoning_exactly_as_before() -> None:
    parser = _parse(
        "deepseek", _reasoning_item(), _function_item("call-read-1", "note.txt")
    )
    assistant = parser.full_message()

    assert AURA_PROVIDER_REASONING_KEY not in assistant
    assert assistant["reasoning_content"] == "Checking the file first."

    _instructions, items = project_responses_input(
        [
            {"role": "user", "content": "read note.txt"},
            assistant,
            {"role": "tool", "tool_call_id": "call-read-1", "content": "contents"},
        ],
        provider="deepseek",
    )
    assert _wire_kinds(items) == [
        "user",
        "function_call",
        "function_call_output",
    ]
    assert "reasoning" not in json.dumps(items)


# ── cancellation / incomplete / failure never complete partial state ────────


def test_cancelled_incomplete_and_failed_turns_store_no_continuation_state() -> None:
    cancelled = ResponsesProductionStreamParser(provider="openai")
    for event in _stream_events(
        _reasoning_item(), _function_item("call-read-1", "note.txt")
    )[:-1]:
        cancelled.push(event)
    cancelled.cancel()
    cancelled_message = cancelled.full_message(include_tool_calls=False)
    assert AURA_PROVIDER_REASONING_KEY not in cancelled_message
    assert "tool_calls" not in cancelled_message

    incomplete = ResponsesProductionStreamParser(provider="openai")
    for event in _stream_events(
        _reasoning_item(), _function_item("call-read-1", "note.txt")
    )[:-1]:
        incomplete.push(event)
    incomplete.push(
        SimpleNamespace(
            type="response.incomplete",
            response=SimpleNamespace(
                id="resp-openai",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                usage=_usage(),
            ),
        )
    )
    assert AURA_PROVIDER_REASONING_KEY not in incomplete.full_message()

    failed = ResponsesProductionStreamParser(provider="openai")
    failed.push(
        SimpleNamespace(
            type="response.failed",
            response=SimpleNamespace(
                id="resp-openai",
                error=SimpleNamespace(code="server_error", message="failed"),
            ),
        )
    )
    assert AURA_PROVIDER_REASONING_KEY not in failed.full_message()


# ── 7/8. model-aware reasoning request shape ───────────────────────────────


@pytest.mark.parametrize(
    ("thinking", "effort"),
    [("off", "none"), ("high", "high"), ("max", "xhigh")],
)
@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.4-mini", "o4-mini"])
def test_openai_reasoning_models_get_the_documented_off_high_max_shape(
    model: str, thinking: str, effort: str
) -> None:
    request = build_responses_request(
        provider="openai",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        hosted_tools=None,
        model=model,
        thinking=thinking,
        temperature=0.25,
    )

    assert request["reasoning"] == {"effort": effort}
    # OpenAI reasoning models do not support temperature in any effort mode.
    assert "temperature" not in request


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o"])
@pytest.mark.parametrize("thinking", ["off", "high", "max"])
def test_non_reasoning_openai_models_omit_the_reasoning_field_entirely(
    model: str, thinking: str
) -> None:
    request = build_responses_request(
        provider="openai",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        hosted_tools=None,
        model=model,
        thinking=thinking,
        temperature=0.25,
    )

    assert "reasoning" not in request
    assert request["temperature"] == 0.25


def test_deepseek_responses_reasoning_request_shape_is_unchanged() -> None:
    for thinking, effort in (("off", "none"), ("high", "high"), ("max", "max")):
        request = build_responses_request(
            provider="deepseek",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            hosted_tools=None,
            model="deepseek-v4-flash",
            thinking=thinking,
            temperature=0.25,
        )
        assert request["reasoning"] == {"effort": effort}
        assert (request.get("temperature") == 0.25) is (thinking == "off")


# ── 9/10. documented hosted-search model gating ────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "gpt-5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.5",
        "gpt-5-pro",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-2025-04-14",
        "gpt-4.1-mini-2025-04-14",
        "GPT-5.5",
        "o4-mini",
        "o4-mini-2025-04-16",
    ],
)
def test_documented_openai_search_models_aliases_and_snapshots_are_admitted(
    model: str,
) -> None:
    capability = native_web_search_capability("openai", model, transport="responses")

    assert capability is not None
    assert capability.tool == {"type": "web_search"}


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4o-2024-08-06",
        "gpt-4o-search-preview",
        "gpt-4.1-nano",
        "gpt-4.1-nano-2025-04-14",
        "gpt-5-search-api",
        "gpt-5-chat-latest",
        "gpt-4-turbo",
        "gpt-5-audio",
        "gpt-5-realtime",
        "gpt-4o-transcribe",
        "gpt-4o-mini-tts",
        "gpt-image-1",
        "o3",
        "o1-mini",
    ],
)
def test_undocumented_and_specialized_openai_models_omit_search_honestly(
    model: str,
) -> None:
    assert native_web_search_capability("openai", model, transport="responses") is None

    request = build_responses_request(
        provider="openai",
        messages=[{"role": "user", "content": "current info"}],
        tools=None,
        hosted_tools=None,
        model=model,
        thinking="high",
    )
    # No hosted tool, and no secondary request or provider fallback either.
    assert "tools" not in request


# ── 15. the split modules have one owner each and no import cycle ──────────


def test_split_responses_modules_have_single_owners_and_no_cycles() -> None:
    assert build_responses_request.__module__ == "aura.client.responses_request"
    assert project_responses_input.__module__ == "aura.client.responses_request"
    assert (
        ResponsesProductionStreamParser.__module__ == "aura.client.responses_stream"
    )

    root = Path(__file__).resolve().parents[1]
    assert not (root / "aura" / "client" / "deepseek_responses.py").exists()
    for name in (
        "responses_request.py",
        "responses_stream.py",
        "responses_transport.py",
        "responses_continuation.py",
        "responses_common.py",
    ):
        source = (root / "aura" / "client" / name).read_text(encoding="utf-8")
        assert len(source.splitlines()) < 500, name

    # Importing each module first, in a fresh interpreter, proves there is no
    # circular dependency between the split owners.
    for module in (
        "aura.client.responses_continuation",
        "aura.client.responses_request",
        "aura.client.responses_stream",
        "aura.client.responses_transport",
    ):
        subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=root,
            check=True,
            capture_output=True,
        )
