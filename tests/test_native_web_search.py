"""Focused tests for native web search execution and tool wiring.

Covers exact request construction at the client boundary, execution and
normalization, timeout and cancellation, HTTP error mapping (400/401/402/
429/500/503), explicit unsupported behavior for providers without native
search, no browser/Drone invocation, and unchanged chat/workspace ownership
and persistence through the standard tool round.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from openai import APIStatusError

from aura.client.events import ApiError, ContentDelta, Done
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry
from aura.research.native import (
    NATIVE_WEB_SEARCH_PROVIDERS,
    execute_native_web_search,
    supports_native_web_search,
    unsupported_web_search_result,
)
from aura.research.policy import decide_research_policy
from aura.research.result import ResearchResult


# ---------------------------------------------------------------------------
# Capability handling
# ---------------------------------------------------------------------------


def test_supports_native_web_search_only_deepseek():
    assert supports_native_web_search("deepseek") is True
    assert supports_native_web_search(None) is False
    for provider in ("openai", "anthropic", "openrouter", "google_cloud", ""):
        assert supports_native_web_search(provider) is False
    assert NATIVE_WEB_SEARCH_PROVIDERS == frozenset({"deepseek"})


def test_unsupported_provider_result_is_explicit(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no client may be constructed for unsupported providers")

    monkeypatch.setattr("aura.research.native.DeepSeekClient", boom)
    result = execute_native_web_search(provider="openai", question="Any news?")
    assert result.ok is False
    assert result.status == "unsupported"
    assert "openai" in result.error
    assert "web_search" in result.error
    assert result.route_used["mode"] == "unsupported"


# ---------------------------------------------------------------------------
# Execution and normalization
# ---------------------------------------------------------------------------


def _fake_completed_stream():
    yield ContentDelta("Ans")
    yield ContentDelta("wer")
    yield Done(
        finish_reason="completed",
        full_message={
            "status": "completed",
            "text": "Answer",
            "sources": [
                {"title": "Example News", "url": "https://example.com/news"}
            ],
            "web_search_calls": [{"item_id": "ws_1", "status": "completed"}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "cache_hit_tokens": 30,
                "cache_miss_tokens": 90,
                "total_tokens": 165,
            },
            "incomplete_reason": None,
            "error": None,
            "error_code": None,
            "response_id": "resp_123",
        },
    )


class FakeClient:
    def __init__(self, provider=None, **kwargs):
        self.provider = provider
        self.stream_events = []

    def stream_responses_web_search(
        self,
        question=None,
        context=None,
        model=None,
        cancel_event=None,
    ):
        for event in self.stream_events:
            yield event


def _patch_client(monkeypatch, events):
    fake = FakeClient()
    fake.stream_events = events
    monkeypatch.setattr(
        "aura.research.native.DeepSeekClient", lambda *a, **k: fake
    )
    return fake


def test_execute_native_web_search_completed(monkeypatch):
    fake = _patch_client(monkeypatch, list(_fake_completed_stream()))
    result = execute_native_web_search(
        provider="deepseek",
        question="Are there any World Cup matches today?",
        model="deepseek-v4-flash",
    )
    assert result.ok is True
    assert result.answer == "Answer"
    assert result.sources == [
        {"title": "Example News", "url": "https://example.com/news"}
    ]
    assert result.confidence == "high"
    assert result.status == "completed"
    assert result.run_id == "resp_123"
    assert result.route_used == {
        "capability": "web_search",
        "provider": "deepseek",
        "mode": "native_responses_web_search",
        "model": "deepseek-v4-flash",
    }
    assert result.usage["prompt_tokens"] == 120
    assert fake.provider == "deepseek"


def test_execute_native_web_search_incomplete(monkeypatch):
    _patch_client(
        monkeypatch,
        [
            Done(
                finish_reason="incomplete",
                full_message={
                    "status": "incomplete",
                    "text": "partial",
                    "sources": [],
                    "web_search_calls": [],
                    "usage": None,
                    "incomplete_reason": "max_output_tokens",
                    "error": None,
                    "error_code": None,
                    "response_id": "resp_2",
                },
            )
        ],
    )
    result = execute_native_web_search(
        provider="deepseek", question="Any news?"
    )
    assert result.ok is False
    assert result.status == "incomplete"
    assert "max_output_tokens" in result.error
    assert result.confidence == "none"


def test_execute_native_web_search_failed(monkeypatch):
    _patch_client(
        monkeypatch,
        [
            Done(
                finish_reason="failed",
                full_message={
                    "status": "failed",
                    "text": "",
                    "sources": [],
                    "web_search_calls": [],
                    "usage": None,
                    "incomplete_reason": None,
                    "error": "upstream search failed",
                    "error_code": "server_error",
                    "response_id": "resp_3",
                },
            )
        ],
    )
    result = execute_native_web_search(
        provider="deepseek", question="Any news?"
    )
    assert result.ok is False
    assert result.status == "failed"
    assert "upstream search failed" in result.error


def test_execute_native_web_search_no_payload(monkeypatch):
    _patch_client(monkeypatch, [ContentDelta("hello")])
    result = execute_native_web_search(
        provider="deepseek", question="Any news?"
    )
    assert result.ok is False
    assert result.status == "failed"
    assert "ended without a response" in result.error


def test_execute_native_web_search_requires_question():
    result = execute_native_web_search(provider="deepseek", question="   ")
    assert result.ok is False
    assert result.status == "invalid_request"


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code",
    [400, 401, 402, 429, 500, 503],
)
def test_http_status_errors_map_to_clear_results(monkeypatch, status_code):
    _patch_client(
        monkeypatch,
        [
            ApiError(
                status_code=status_code,
                message=f"fake HTTP {status_code}",
            )
        ],
    )
    result = execute_native_web_search(
        provider="deepseek", question="Any news?"
    )
    assert result.ok is False
    assert result.status == "failed"
    assert f"HTTP {status_code}" in result.error


def test_http_status_error_hints(monkeypatch):
    hints = {
        400: "rejected",
        401: "authentication",
        402: "payment",
        429: "rate limited",
        500: "internal error",
        503: "unavailable",
    }
    for code, hint in hints.items():
        _patch_client(
            monkeypatch,
            [ApiError(status_code=code, message=f"fake {code}")],
        )
        result = execute_native_web_search(
            provider="deepseek", question="Any news?"
        )
        assert hint in result.error, f"missing hint for {code}: {result.error}"


def test_client_maps_api_status_error_to_api_error_event(monkeypatch):
    from aura.client.deepseek import DeepSeekClient

    def raise_status_error(**kwargs):
        raise APIStatusError(
            "fake 429",
            response=_fake_http_response(429),
            body=None,
        )

    fake_raw = type("FakeRaw", (), {"responses": type("FakeResponses", (), {"create": staticmethod(raise_status_error)})()})()
    client = DeepSeekClient(api_key="test-key", provider="deepseek")
    monkeypatch.setattr(client, "_client", fake_raw)

    events = list(
        client.stream_responses_web_search(
            question="Any news?", model="deepseek-v4-flash"
        )
    )
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ApiError)
    assert error.status_code == 429


# ---------------------------------------------------------------------------
# Client request construction
# ---------------------------------------------------------------------------


def _fake_http_response(status_code: int):
    import httpx

    return httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://api.deepseek.com/responses"),
    )


def test_client_stream_responses_builds_exact_request(monkeypatch):
    from aura.client.deepseek import DeepSeekClient

    captured = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return iter([])

    fake_raw = type("FakeRaw", (), {"responses": FakeResponses()})()
    client = DeepSeekClient(api_key="test-key-123", provider="deepseek")
    monkeypatch.setattr(client, "_client", fake_raw)

    events = list(
        client.stream_responses_web_search(
            question="Are there any World Cup matches today?",
            context="User is in London.",
            model="deepseek-v4-flash",
        )
    )

    assert client._base_url == "https://api.deepseek.com"
    assert client._api_key == "test-key-123"
    assert captured == {
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
        "tool_choice": "auto",
        "stream": True,
    }
    # Empty stream still terminates with a Done event carrying the neutral payload.
    assert isinstance(events[-1], Done)
    assert events[-1].full_message["status"] == "in_progress"


def test_client_stream_resolves_default_model(monkeypatch):
    from aura.client.deepseek import DeepSeekClient

    captured = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return iter([])

    fake_raw = type("FakeRaw", (), {"responses": FakeResponses()})()
    client = DeepSeekClient(api_key="test-key", provider="deepseek")
    monkeypatch.setattr(client, "_client", fake_raw)

    list(client.stream_responses_web_search(question="Any news?"))
    assert captured["model"] == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Timeout and cancellation
# ---------------------------------------------------------------------------


def _never_ending_stream():
    while True:
        time.sleep(0.05)


def test_client_stream_first_event_timeout(monkeypatch):
    import time

    from aura.client import deepseek as deepseek_module
    from aura.client.deepseek import DeepSeekClient

    class FakeResponses:
        def create(self, **kwargs):
            return _never_ending_stream()

    fake_raw = type("FakeRaw", (), {"responses": FakeResponses()})()
    client = DeepSeekClient(api_key="test-key", provider="deepseek")
    monkeypatch.setattr(client, "_client", fake_raw)
    monkeypatch.setattr(
        deepseek_module, "FIRST_STREAM_EVENT_TIMEOUT_SECONDS", 0.2
    )

    events = list(
        client.stream_responses_web_search(
            question="Any news?", model="deepseek-v4-flash"
        )
    )
    assert isinstance(events[-1], Done)
    assert events[-1].full_message["status"] == "in_progress"


def test_client_stream_cancellation(monkeypatch):
    from aura.client.deepseek import DeepSeekClient

    class FakeResponses:
        def create(self, **kwargs):
            return _never_ending_stream()

    fake_raw = type("FakeRaw", (), {"responses": FakeResponses()})()
    client = DeepSeekClient(api_key="test-key", provider="deepseek")
    monkeypatch.setattr(client, "_client", fake_raw)

    cancel_event = threading.Event()
    cancel_event.set()
    events = list(
        client.stream_responses_web_search(
            question="Any news?",
            model="deepseek-v4-flash",
            cancel_event=cancel_event,
        )
    )
    assert isinstance(events[-1], Done)
    assert events[-1].full_message["status"] == "cancelled"


def test_execute_native_web_search_cancellation(monkeypatch):
    _patch_client(
        monkeypatch,
        [
            Done(
                finish_reason="cancelled",
                full_message={
                    "status": "cancelled",
                    "text": "",
                    "sources": [],
                    "web_search_calls": [],
                    "usage": None,
                    "incomplete_reason": None,
                    "error": None,
                    "error_code": None,
                    "response_id": "",
                },
            )
        ],
    )
    result = execute_native_web_search(
        provider="deepseek",
        question="Any news?",
        cancel_event=threading.Event(),
    )
    assert result.ok is False
    assert "cancelled" in result.error


# ---------------------------------------------------------------------------
# Tool wiring: handler, catalog, registry
# ---------------------------------------------------------------------------


def _round_runner(tmp_path, history: History | None = None):
    history = history or History()
    registry = ToolRegistry(tmp_path, mode="planner")
    registry.set_provider("deepseek")
    tool_runner = ToolRunner(history, tmp_path)
    return (
        ToolRoundRunner(
            history=history,
            tools=registry,
            tool_runner=tool_runner,
            planner_refresh=PlannerRefreshState(),
        ),
        history,
        registry,
    )


def test_web_search_handler_is_registered():
    assert "web_search" in TOOL_HANDLERS
    assert TOOL_HANDLERS["web_search"] == ToolRegistry._handle_web_search


def test_web_search_tool_in_catalog_for_all_active_modes():
    catalog = ToolCatalog()
    for mode in ("planner", "worker", "single"):
        names = {
            tool["function"]["name"]
            for tool in catalog.build_tool_defs(mode=mode, read_only=False)
        }
        assert "web_search" in names, f"missing in {mode}"
    read_only_names = {
        tool["function"]["name"]
        for tool in catalog.build_tool_defs(mode="single", read_only=True)
    }
    assert "web_search" not in read_only_names


def test_web_search_handler_runs_and_never_launches_drone_or_browser(
    tmp_path, monkeypatch
):
    def assert_no_drone(*args, **kwargs):
        raise AssertionError("drone path must not be invoked")

    def assert_no_browser(*args, **kwargs):
        raise AssertionError("browser path must not be invoked")

    monkeypatch.setattr("aura.drones.store.DroneStore.load_drone", assert_no_drone)
    monkeypatch.setattr(
        "aura.drones.sync_runner.run_read_only_drone_sync", assert_no_drone
    )
    monkeypatch.setattr(
        "aura.drones.background_runner.get_background_runner", assert_no_drone
    )

    captured = {}

    def fake_execute(*, provider, question, context=None, model=None, cancel_event=None):
        captured.update(
            provider=provider, question=question, context=context
        )
        return ResearchResult(
            ok=True,
            answer="42",
            sources=[{"title": "T", "url": "https://u"}],
            confidence="high",
            status="completed",
        )

    monkeypatch.setattr(
        "aura.conversation.tools._planner_mixin.execute_native_web_search",
        fake_execute,
    )
    registry = ToolRegistry(tmp_path, mode="planner")
    registry.set_provider("deepseek")

    result = registry._handle_web_search(
        {"question": "Meaning of life?", "context": "local hint"},
        approval_cb=None,
        reject_all=False,
    )
    assert result.ok is True
    assert result.payload["ok"] is True
    assert result.payload["answer"] == "42"
    assert "answer_for_chat" in result.payload
    assert captured == {
        "provider": "deepseek",
        "question": "Meaning of life?",
        "context": "local hint",
    }
    # workspace untouched
    assert list(tmp_path.iterdir()) == []


def test_web_search_handler_unsupported_provider_explicit(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("native executor must not run for unsupported providers")

    monkeypatch.setattr(
        "aura.conversation.tools._planner_mixin.execute_native_web_search", boom
    )
    registry = ToolRegistry(tmp_path, mode="planner")
    registry.set_provider("openai")
    result = registry._handle_web_search(
        {"question": "Any news?"}, approval_cb=None, reject_all=False
    )
    assert result.ok is True
    assert result.payload["ok"] is False
    assert result.payload["status"] == "unsupported"
    assert "openai" in result.payload["error"]
    assert "web_search" in result.payload["error"]


def test_web_search_handler_requires_question(tmp_path):
    registry = ToolRegistry(tmp_path, mode="planner")
    result = registry._handle_web_search(
        {"question": "  "}, approval_cb=None, reject_all=False
    )
    assert result.ok is False
    assert "question is required" in result.payload["error"]


def test_web_research_drone_id_is_retired(tmp_path):
    registry = ToolRegistry(tmp_path, mode="planner")
    for handler_name in ("run_read_only_drone", "launch_read_only_drone"):
        handler = getattr(registry, f"_handle_{handler_name}")
        result = handler(
            {"drone_id": "web-research", "goal": "Any news?"},
            approval_cb=None,
            reject_all=False,
        )
        assert result.ok is True
        assert result.payload["ok"] is False
        assert result.payload["status"] == "unsupported"
        assert "web_search" in result.payload["error"]


def test_web_search_tool_round_preserves_chat_and_persistence(tmp_path, monkeypatch):
    """A web_search tool call flows through the standard tool round and
    appends to history exactly like any other tool — chat ownership and
    persistence mechanics are unchanged."""
    runner, history, registry = _round_runner(tmp_path)

    def fake_execute(*, provider, question, context=None, model=None, cancel_event=None):
        return ResearchResult(
            ok=True,
            answer="The latest Aura version is 1.9.18.",
            sources=[{"title": "Aura", "url": "https://example.com/aura"}],
            confidence="high",
            status="completed",
        )

    monkeypatch.setattr(
        "aura.conversation.tools._planner_mixin.execute_native_web_search",
        fake_execute,
    )

    tool_call = {
        "id": "call_ws_1",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"question": "What is the latest Aura version?"}),
        },
    }
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": None,
            "tool_calls": [tool_call],
        }
    )
    state = _SendState(
        mode="planner",
        research_policy=decide_research_policy("What is the latest Aura version?"),
    )
    outcome = runner.run(
        tool_calls=[tool_call],
        state=state,
        on_event=lambda event: None,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        dispatch_cb=None,
        cleanup_cancelled=lambda on_event: None,
    )
    assert outcome.action in ("return", "continue")

    last = history.messages[-1]
    assert last["role"] == "tool"
    payload = json.loads(last["content"])
    assert payload["ok"] is True
    assert payload["answer"] == "The latest Aura version is 1.9.18."
    assert "answer_for_chat" in payload
    # workspace untouched by the research flow
    assert list(tmp_path.iterdir()) == []
