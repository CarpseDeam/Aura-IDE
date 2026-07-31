"""Tests for the research adapter and native web search seam.

The legacy browser/Drone web-research path is retired.  The adapter resolves
research requests through Aura's own search backend (DeepSeek's native
Responses API web search) regardless of the selected chat model provider —
the old browser/Drone system is never launched.
"""

from __future__ import annotations

from aura.research.adapter import (
    WEB_RESEARCH_DRONE_ID,
    build_adapter_call,
    execute_web_research_request,
)
from aura.research.request import build_research_request
from aura.research.result import ResearchResult


def test_answer_only_adapter_call_still_builds_compat_call():
    """build_adapter_call remains a compatibility seam with the legacy id."""
    request = build_research_request("Are there any World Cup matches today?")

    call = build_adapter_call(request)

    assert call.drone_id == WEB_RESEARCH_DRONE_ID
    assert call.goal == "Are there any World Cup matches today?"


def test_execute_web_research_request_uses_native_search(tmp_path, monkeypatch):
    """The adapter resolves through the native executor, not the Drone."""
    from aura.research import adapter as adapter_module

    captured = {}

    def fake_native(
        *, question, context=None, model=None, cancel_event=None, chat_provider=None
    ):
        captured.update(
            question=question, context=context, chat_provider=chat_provider
        )
        return ResearchResult(
            ok=True,
            answer="42",
            sources=[{"title": "T", "url": "https://u"}],
            confidence="high",
            status="completed",
        )

    monkeypatch.setattr(adapter_module, "execute_native_web_search", fake_native)

    request = build_research_request("Are there any World Cup matches today?")
    result = execute_web_research_request(tmp_path, request)

    assert result["ok"] is True
    assert result["answer"] == "42"
    assert result["status"] == "completed"
    assert captured["question"] == "Are there any World Cup matches today?"


def test_execute_web_research_request_ignores_chat_provider(tmp_path, monkeypatch):
    """A non-DeepSeek chat provider still runs the DeepSeek-backed search."""
    from aura.research import adapter as adapter_module

    captured = {}

    def fake_native(
        *, question, context=None, model=None, cancel_event=None, chat_provider=None
    ):
        captured.update(chat_provider=chat_provider)
        return ResearchResult(
            ok=True,
            answer="42",
            sources=[{"title": "T", "url": "https://u"}],
            confidence="high",
            status="completed",
        )

    monkeypatch.setattr(adapter_module, "execute_native_web_search", fake_native)

    request = build_research_request("Are there any World Cup matches today?")
    result = execute_web_research_request(
        tmp_path, request, chat_provider="openrouter"
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    # The chat provider is metadata only — it never blocks search.
    assert captured["chat_provider"] == "openrouter"


def test_execute_web_research_request_requires_question(tmp_path):
    request = build_research_request("   ")
    result = execute_web_research_request(tmp_path, request)

    assert result["ok"] is False
    assert result["status"] == "invalid_request"


def test_execute_web_research_request_never_launches_drone(tmp_path, monkeypatch):
    """The Drone sync runner is never invoked by the research seam."""
    from aura.drones import sync_runner
    from aura.research import adapter as adapter_module

    def assert_no_drone(*args, **kwargs):
        raise AssertionError("Drone runner must not be invoked for research")

    monkeypatch.setattr(
        "aura.drones.sync_runner.run_read_only_drone_sync", assert_no_drone
    )
    monkeypatch.setattr("aura.drones.store.DroneStore.load_drone", assert_no_drone)
    monkeypatch.setattr(
        adapter_module,
        "execute_native_web_search",
        lambda **kwargs: ResearchResult(ok=True, answer="42", status="completed"),
    )

    request = build_research_request("Are there any World Cup matches today?")
    result = execute_web_research_request(
        tmp_path, request, chat_provider="openai"
    )

    assert result["ok"] is True
    assert sync_runner.run_read_only_drone_sync is assert_no_drone
