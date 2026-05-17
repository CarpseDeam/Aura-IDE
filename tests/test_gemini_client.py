"""Tests for the native Gemini API client."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aura.backends.api import APIAgentBackend
from aura.client.events import ContentDelta, Done, ToolCallArgsDelta, ToolCallStart, Usage
from aura.client.gemini import (
    GeminiClient,
    _to_gemini_contents,
    _to_gemini_tools,
)


def test_api_backend_uses_native_gemini_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    backend = APIAgentBackend(provider="google")

    assert isinstance(backend.client, GeminiClient)


def test_to_gemini_contents_translates_history_and_tool_results() -> None:
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

    system, contents = _to_gemini_contents(messages)

    assert system == {"parts": [{"text": "You are concise."}]}
    assert contents == [
        {"role": "user", "parts": [{"text": "Use the tool."}]},
        {
            "role": "model",
            "parts": [
                {
                    "function_call": {
                        "name": "read_file",
                        "args": {"path": "a.py"},
                    }
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


def test_to_gemini_tools_translates_openai_tool_schema() -> None:
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

    assert _to_gemini_tools(tools) == [
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


def test_gemini_stream_yields_text_tool_calls_usage_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> list[str]:
            chunks = [
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
                                        "functionCall": {
                                            "name": "read_file",
                                            "args": {"path": "a.py"},
                                        }
                                    }
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "cachedContentTokenCount": 4,
                        "candidatesTokenCount": 3,
                    },
                },
            ]
            lines: list[str] = []
            for chunk in chunks:
                lines.extend([f"data: {json.dumps(chunk)}", ""])
            return lines

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr("aura.client.gemini.httpx.Client", FakeClient)

    events = list(
        GeminiClient().stream(
            messages=[{"role": "user", "content": "hello"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
            model="gemini-2.0-flash",
            thinking="off",
            temperature=0.25,
        )
    )

    assert captured["method"] == "POST"
    # Verify the URL has the hardcoded query string starting with '?'
    url_str = str(captured["url"])
    assert ":streamGenerateContent?alt=sse&key=test-key" in url_str
    assert url_str.endswith("/models/gemini-2.0-flash:streamGenerateContent?alt=sse&key=test-key")
    # params should be empty or not present in kwargs now
    assert "params" not in captured["kwargs"]
    body = captured["kwargs"]["json"]
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert body["generation_config"] == {"temperature": 0.25}
    assert body["tool_config"] == {"function_calling_config": {"mode": "auto"}}

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
    assert done.full_message["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path": "a.py"}',
    }


def test_gemini_model_url_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    captured_urls = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse": return self
        def __exit__(self, *args: object) -> None: pass
        def raise_for_status(self) -> None: pass
        def iter_lines(self) -> list[str]: return []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None: pass
        def __enter__(self) -> "FakeClient": return self
        def __exit__(self, *args: object) -> None: pass
        def stream(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            captured_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr("aura.client.gemini.httpx.Client", FakeClient)

    client = GeminiClient()
    # Case 1: Bare model ID
    list(client.stream([], None, "gemini-3.1-pro-preview", "off"))
    # Case 2: Prefixed model ID
    list(client.stream([], None, "models/gemini-3.1-pro-preview", "off"))

    url0 = str(captured_urls[0])
    url1 = str(captured_urls[1])
    assert "/models/models/" not in url1
    assert url0.endswith("/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse&key=test-key")
    assert url1.endswith("/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse&key=test-key")
