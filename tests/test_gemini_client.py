"""Tests for the Google Gen AI Gemini client."""

from __future__ import annotations

import json
from typing import Any

import pytest

import aura.client.gemini as gemini_mod
from google import genai as real_genai
from aura.backends.api import APIAgentBackend
from aura.client.events import ContentDelta, Done, ToolCallArgsDelta, ToolCallStart, Usage
from aura.client.gemini import (
    GeminiClient,
    _generation_config,
    _json_safe,
    _to_genai_contents,
    _to_genai_tools,
)


class _FakeModels:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    def list(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._owner.captured["list_kwargs"] = kwargs
        return [
            {"name": "publishers/google/models/gemini-2.5-flash"},
            {"name": "publishers/google/models/imagen-4.0-generate-preview"},
        ]

    def generate_content_stream(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._owner.captured["stream_kwargs"] = kwargs
        return self._owner.stream_chunks


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.captured: dict[str, Any] = {}
        self.stream_chunks: list[dict[str, Any]] = []
        self.models = _FakeModels(self)
        _FakeClient.instances.append(self)


class _FakeGenAI:
    Client = _FakeClient


@pytest.fixture(autouse=True)
def fake_genai(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr(gemini_mod, "genai", _FakeGenAI)
    monkeypatch.setattr(gemini_mod, "genai_types", None)
    monkeypatch.setattr(gemini_mod, "HAS_GOOGLE_GENAI", True)


def test_api_backend_uses_google_ai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test-key")
    backend = APIAgentBackend(provider="google_ai")
    assert isinstance(backend.client, GeminiClient)
    assert backend.client.vertexai is False
    assert backend.client.credential == "AIza-test-key"


def test_api_backend_uses_vertex_ai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    backend = APIAgentBackend(provider="vertex_ai")
    assert isinstance(backend.client, GeminiClient)
    assert backend.client.vertexai is True
    assert backend.client.credential == "test-project"


def test_google_ai_client_uses_api_key() -> None:
    client = GeminiClient(credential="test-key", vertexai=False)
    sdk_client = client._make_sdk_client()

    assert sdk_client.kwargs["vertexai"] is False
    assert sdk_client.kwargs["api_key"] == "test-key"
    assert "project" not in sdk_client.kwargs


def test_vertex_ai_client_supports_api_key() -> None:
    client = GeminiClient(credential="AIza-vertex-key", vertexai=True)
    sdk_client = client._make_sdk_client()

    assert sdk_client.kwargs["vertexai"] is True
    assert sdk_client.kwargs["api_key"] == "AIza-vertex-key"
    assert "project" not in sdk_client.kwargs


def test_vertex_discovery_falls_back_to_google_ai_on_401_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiClient(credential="AIza-test-key", vertexai=True)

    calls = []

    def fake_list(self, **kwargs):
        is_vertex = self._owner.kwargs["vertexai"]
        calls.append(is_vertex)
        if is_vertex:
            # Simulate Vertex 401
            raise RuntimeError("401 UNAUTHENTICATED: Principal required")
        return [{"name": "models/gemini-2.0-flash"}]

    monkeypatch.setattr(_FakeModels, "list", fake_list)

    models = client.fetch_raw_models()

    # Should have called Vertex first (True), then Google AI (False)
    assert calls == [True, False]
    assert models[0]["id"] == "gemini-2.0-flash"


def test_vertex_ai_client_uses_project_and_overrides_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure env is "polluted"
    monkeypatch.setenv("GOOGLE_API_KEY", "STRAY_KEY_SHOULD_BE_IGNORED")

    client = GeminiClient(credential="test-project", vertexai=True)
    sdk_client = client._make_sdk_client()

    assert sdk_client.kwargs["vertexai"] is True
    assert sdk_client.kwargs["project"] == "test-project"
    # Overridden to None to prevent SDK from picking up environment
    assert sdk_client.kwargs["api_key"] is None


def test_to_genai_contents_translates_history_and_tool_results() -> None:
    messages = [
        {"role": "system", "content": "You are concise."},
        {"role": "user", "content": "Use the tool."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
    ]

    system, contents = _to_genai_contents(messages)

    assert system == "You are concise."
    assert contents == [
        {"role": "user", "parts": [{"text": "Use the tool."}]},
        {
            "role": "model",
            "parts": [
                {
                    "function_call": {
                        "name": "read_file",
                        "args": {"path": "a.py"},
                    },
                    "thought_signature": "skip_thought_signature_validator",
                }
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": "read_file",
                        "response": {"ok": True},
                    }
                }
            ],
        },
    ]


def test_to_genai_contents_preserves_function_call_thought_signature() -> None:
    messages = [
        {"role": "user", "content": "Use the tool."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "thought_signature": "signed-call",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
    ]

    _system, contents = _to_genai_contents(messages)

    assert contents[1]["parts"][0]["thought_signature"] == "signed-call"


def test_to_genai_contents_does_not_add_placeholder_to_plain_text() -> None:
    _system, contents = _to_genai_contents(
        [{"role": "assistant", "content": "Done."}]
    )

    assert contents == [{"role": "model", "parts": [{"text": "Done."}]}]


def test_to_genai_contents_reuses_message_signature_for_first_unsigned_tool_call() -> None:
    messages = [
        {"role": "user", "content": "Use the tool."},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Thinking",
            "thought_signature": "text-signature",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
    ]

    _system, contents = _to_genai_contents(messages)

    model_parts = contents[1]["parts"]
    assert model_parts[0]["thought_signature"] == "text-signature"
    assert model_parts[1]["thought_signature"] == "text-signature"


def test_gemini_byte_thought_signatures_are_json_safe_for_history_and_request() -> None:
    raw_message = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "Thinking",
        "thought_signature": b"\x00\x01abc",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "thought_signature": b"\x02\x03def",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            }
        ],
    }
    tool_message = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok":true}',
    }

    encoded_message = _json_safe(raw_message)
    json.dumps(encoded_message)
    assert encoded_message["thought_signature"] == "base64:AAFhYmM="
    assert encoded_message["tool_calls"][0]["thought_signature"] == "base64:AgNkZWY="

    _system, contents = _to_genai_contents([raw_message, tool_message])

    assert _contains_bytes(contents) is False
    assert contents[0]["parts"][0]["thought_signature"] == "AAFhYmM="
    assert contents[0]["parts"][1]["thought_signature"] == "AgNkZWY="
    json.dumps(contents)


def test_to_genai_tools_uses_sdk_field_names() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]

    assert _to_genai_tools(tools) == [
        {
            "function_declarations": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ]
        }
    ]


def test_generation_config_uses_google_genai_field_names() -> None:
    config = _generation_config(
        thinking="high",
        temperature=0.25,
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
        system_instruction="Be brief.",
    )

    assert config["system_instruction"] == "Be brief."
    assert config["temperature"] == 0.25
    assert config["candidate_count"] == 1
    assert config["thinking_config"] == {
        "include_thoughts": True,
        "thinking_budget": 8192,
    }
    assert config["tool_config"] == {"function_calling_config": {"mode": "AUTO"}}
    assert any(s["category"] == "HARM_CATEGORY_DANGEROUS_CONTENT" for s in config["safety_settings"])


def test_gemini_stream_yields_text_tool_calls_usage_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiClient(credential="test-key", vertexai=False)

    sdk_client = _FakeClient()
    sdk_client.stream_chunks = [
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "Hi"}]},
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "function_call": {
                                    "name": "read_file",
                                    "args": {"path": "a.py"},
                                },
                                "thought_signature": "tool-signature",
                            }
                        ]
                    },
                    "finish_reason": "STOP",
                }
            ],
            "usage_metadata": {
                "prompt_token_count": 10,
                "cached_content_token_count": 4,
                "candidates_token_count": 3,
            },
        },
    ]
    monkeypatch.setattr(client, "_make_sdk_client", lambda: sdk_client)

    events = list(
        client.stream(
            messages=[{"role": "user", "content": "hello"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
            model="models/gemini-2.0-flash",
            thinking="off",
            temperature=0.25,
        )
    )

    assert sdk_client.captured["stream_kwargs"]["model"] == "gemini-2.0-flash"
    assert sdk_client.captured["stream_kwargs"]["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]}
    ]
    assert sdk_client.captured["stream_kwargs"]["config"]["temperature"] == 0.25

    assert any(isinstance(ev, ContentDelta) and ev.text == "Hi" for ev in events)
    assert any(isinstance(ev, ToolCallStart) and ev.name == "read_file" for ev in events)
    assert any(
        isinstance(ev, ToolCallArgsDelta) and ev.args_chunk == '{"path": "a.py"}'
        for ev in events
    )
    assert any(
        isinstance(ev, Usage)
        and ev.prompt_tokens == 10
        and ev.cache_hit_tokens == 4
        and ev.cache_miss_tokens == 6
        and ev.completion_tokens == 3
        for ev in events
    )
    done = next(ev for ev in events if isinstance(ev, Done))
    assert done.finish_reason == "tool_calls"
    assert done.full_message["content"] == "Hi"
    assert done.full_message["thought_signature"] == "tool-signature"
    assert done.full_message["tool_calls"][0]["thought_signature"] == "tool-signature"
    assert done.full_message["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path": "a.py"}',
    }


def test_gemini_stream_encodes_byte_signatures_before_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GeminiClient(credential="test-key", vertexai=True)

    sdk_client = _FakeClient()
    sdk_client.stream_chunks = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "function_call": {
                                    "name": "read_file",
                                    "args": {"path": "a.py", "payload": b"\x04"},
                                    "thoughtSignature": bytearray(b"\x02\x03def"),
                                },
                                "thought_signature": b"\x00\x01abc",
                            }
                        ]
                    }
                }
            ]
        }
    ]
    monkeypatch.setattr(client, "_make_sdk_client", lambda: sdk_client)

    events = list(
        client.stream(
            messages=[{"role": "user", "content": "hello"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
            model="models/gemini-2.0-flash",
            thinking="high",
        )
    )

    done = next(ev for ev in events if isinstance(ev, Done))
    args = json.loads(done.full_message["tool_calls"][0]["function"]["arguments"])

    assert _contains_bytes(done.full_message) is False
    assert done.full_message["thought_signature"] == "base64:AAFhYmM="
    assert done.full_message["tool_calls"][0]["thought_signature"] == "base64:AgNkZWY="
    assert args == {"path": "a.py", "payload": "base64:BA=="}
    json.dumps(done.full_message)


def _contains_bytes(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray)):
        return True
    if isinstance(value, dict):
        return any(_contains_bytes(k) or _contains_bytes(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_bytes(item) for item in value)
    if isinstance(value, tuple):
        return any(_contains_bytes(item) for item in value)
    return False
