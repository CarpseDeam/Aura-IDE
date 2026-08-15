"""Thinking-mode vocabulary, provider mapping, and persistence contracts."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from aura.client import ContentDelta, Done, ToolCallStart
from aura.client import reasoning as reasoning_module
from aura.client.anthropic_stream import (
    _anthropic_effort_policy,
    _anthropic_thinking_config,
)
from aura.client.reasoning import (
    EFFORT_EXPLICIT,
    EFFORT_OMITTED_DISABLED,
    resolve_reasoning_request,
)
from aura.config import DEFAULT_THINKING
from aura.conversation import ConversationManager, History
from aura.conversation.tools import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from aura.providers.base import THINKING_MODES
from aura.settings import AppSettings

ALL_MODES = ("off", "high", "max")


def test_the_three_modes_are_off_high_max() -> None:
    assert THINKING_MODES == ALL_MODES
    assert DEFAULT_THINKING == "high"


def test_gui_selector_offers_exactly_the_three_modes_in_order() -> None:
    from aura.gui.left_pane import _THINKING_LABELS

    assert list(_THINKING_LABELS) == list(THINKING_MODES)
    assert [_THINKING_LABELS[mode] for mode in THINKING_MODES] == [
        "Off", "High", "Max",
    ]


def _build_kwargs(provider: str, thinking: str, temperature: float = 0.7) -> dict[str, Any]:
    """Reproduce the reasoning-related kwargs sent by the client."""
    kwargs: dict[str, Any] = {"model": "m", "messages": [], "stream": True}
    request = resolve_reasoning_request(provider, thinking)
    if request.extra_body is not None:
        kwargs["extra_body"] = request.extra_body
    if request.reasoning_effort is not None:
        kwargs["reasoning_effort"] = request.reasoning_effort
    if request.send_temperature:
        kwargs["temperature"] = temperature
    return kwargs


@pytest.mark.parametrize("provider", ["deepseek", "openai", "openrouter"])
@pytest.mark.parametrize("mode", ALL_MODES)
def test_explicit_mode_is_sent_verbatim(provider: str, mode: str) -> None:
    request = resolve_reasoning_request(provider, mode)

    assert request.thinking == mode
    if mode == "off":
        assert request.reasoning_effort is None
        assert request.effort_policy == EFFORT_OMITTED_DISABLED
    else:
        assert request.reasoning_effort == mode
        assert request.effort_policy == EFFORT_EXPLICIT


def test_legacy_auto_request_is_normalized_to_high() -> None:
    request = resolve_reasoning_request("deepseek", "auto")

    assert request.thinking == "high"
    assert request.reasoning_effort == "high"
    assert _build_kwargs("deepseek", "auto")["reasoning_effort"] == "high"


def test_unknown_request_uses_high_without_provider_side_selection() -> None:
    request = resolve_reasoning_request("openai", "something-new")

    assert request.thinking == "high"
    assert request.reasoning_effort == "high"


def test_reasoning_resolver_has_no_mutable_escalation_state() -> None:
    mutable = {
        name: value
        for name, value in vars(reasoning_module).items()
        if not name.startswith("__")
        and isinstance(value, (list, dict, set, bytearray))
    }
    assert mutable == {}


@pytest.mark.parametrize("mode", ["high", "max"])
def test_anthropic_adaptive_models_receive_explicit_effort(mode: str) -> None:
    config = _anthropic_thinking_config("claude-sonnet-4-6", mode)

    assert config["thinking"]["type"] == "adaptive"
    assert config["output_config"] == {"effort": mode}
    assert _anthropic_effort_policy("claude-sonnet-4-6", mode) == EFFORT_EXPLICIT


def test_anthropic_legacy_auto_uses_high_mapping() -> None:
    assert _anthropic_thinking_config("claude-sonnet-4-6", "auto") == (
        _anthropic_thinking_config("claude-sonnet-4-6", "high")
    )
    assert _anthropic_effort_policy("claude-sonnet-4-6", "auto") == EFFORT_EXPLICIT


def test_anthropic_budget_only_models_keep_high_and_max_budgets() -> None:
    high = _anthropic_thinking_config("claude-haiku-4-5", "high")
    max_mode = _anthropic_thinking_config("claude-haiku-4-5", "max")

    assert high["thinking"]["budget_tokens"] == 10000
    assert max_mode["thinking"]["budget_tokens"] == 32000
    assert _anthropic_thinking_config("claude-haiku-4-5", "auto") == high


def test_gemini_legacy_auto_uses_high_budget() -> None:
    from aura.providers.google_cloud.client import _google_thinking_config

    class _Types:
        class ThinkingConfig:
            def __init__(self, thinking_budget: int, include_thoughts: bool) -> None:
                self.thinking_budget = thinking_budget
                self.include_thoughts = include_thoughts

    legacy = _google_thinking_config(_Types, "gemini-2.5-pro", "auto")
    high = _google_thinking_config(_Types, "gemini-2.5-pro", "high")

    assert legacy.thinking_budget == 8192
    assert legacy.include_thoughts is True
    assert legacy.thinking_budget == high.thinking_budget


class TestSettingsPersistence:
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_explicit_modes_round_trip(self, mode: str) -> None:
        assert AppSettings.from_dict({"default_thinking": mode}).default_thinking == mode

    def test_legacy_auto_loads_as_high(self) -> None:
        assert AppSettings.from_dict({"default_thinking": "auto"}).default_thinking == "high"

    def test_unset_or_invalid_settings_default_to_high(self) -> None:
        assert AppSettings.from_dict({}).default_thinking == "high"
        assert AppSettings.from_dict({"default_thinking": "unknown"}).default_thinking == "high"


class TestConversationPersistence:
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_explicit_mode_round_trips(self, mode: str, tmp_path) -> None:
        from aura.conversation.persistence import load_conversation, save_conversation

        history = History()
        history.set_system("system")
        history.append_user_text("hello")

        path = save_conversation(
            history, tmp_path, model="deepseek-v4-flash", thinking=mode
        )

        assert load_conversation(path).thinking == mode

    def test_legacy_auto_loads_as_high_and_is_saved_as_high(self, tmp_path) -> None:
        from aura.conversation.persistence import load_conversation, save_conversation

        history = History()
        history.set_system("system")
        history.append_user_text("hello")
        path = save_conversation(
            history, tmp_path, model="deepseek-v4-flash", thinking="auto"
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["thinking"] == "high"
        payload["thinking"] = "auto"
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert load_conversation(path).thinking == "high"


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.mark.parametrize("mode", ALL_MODES)
def test_explicit_mode_is_unchanged_across_tool_rounds(
    tmp_path, isolated_streams: ModelStreamRegistry, mode: str
) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    def stream(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            yield ToolCallStart(index=0, id="read-1", name="read_file")
            yield Done(
                finish_reason="tool_calls",
                full_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "read-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "note.txt"}),
                        },
                    }],
                },
            )
            return
        yield ContentDelta("done")
        yield Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "done"},
        )

    isolated_streams.register(PRODUCTION_STREAM_HOOK, stream)
    history = History()
    history.append_user_text("Read note.txt")
    manager = ConversationManager(history, ToolRegistry(tmp_path))
    manager.send(
        on_event=lambda _event: None,
        approval_cb=lambda _request: None,
        cancel_event=threading.Event(),
        model="test-model",
        thinking=mode,
    )

    assert [call["thinking"] for call in calls] == [mode, mode]
