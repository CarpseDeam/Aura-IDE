"""Provider-native ``Auto`` reasoning.

The user picks one of ``off · auto · high · max``. ``auto`` means *the provider
decides* — Aura sends the provider's own documented automatic/adaptive mode, or
omits the effort parameter so the provider applies its documented default.

What is asserted here:

* DeepSeek ``auto`` enables thinking and omits ``reasoning_effort`` entirely;
* explicit ``high``/``max`` still send exactly those values;
* ``off`` is unchanged;
* settings and conversation persistence round-trip ``auto``;
* an existing explicit selection is never silently migrated to ``auto``;
* no Aura-side complexity router or escalation ladder was introduced.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from aura.client import reasoning as reasoning_module
from aura.client.anthropic_stream import (
    _anthropic_effort_policy,
    _anthropic_thinking_config,
)
from aura.client.reasoning import (
    EFFORT_EXPLICIT,
    EFFORT_OMITTED_DISABLED,
    EFFORT_OMITTED_PROVIDER_AUTO,
    EFFORT_OMITTED_PROVIDER_DEFAULT,
    resolve_reasoning_request,
)
from aura.providers.base import THINKING_MODES
from aura.settings import AppSettings

ALL_MODES = ("off", "auto", "high", "max")


# ── the selector vocabulary ─────────────────────────────────────────────────


def test_the_four_modes_are_off_auto_high_max() -> None:
    assert THINKING_MODES == ALL_MODES


def test_gui_selector_offers_exactly_those_four_in_order() -> None:
    from aura.gui.left_pane import _THINKING_LABELS

    assert list(_THINKING_LABELS) == list(THINKING_MODES)
    assert [_THINKING_LABELS[m] for m in THINKING_MODES] == [
        "Off", "Auto", "High", "Max",
    ]


# ── DeepSeek: auto omits reasoning_effort ───────────────────────────────────


class TestDeepSeekAuto:

    def test_auto_enables_thinking_but_omits_reasoning_effort(self) -> None:
        req = resolve_reasoning_request("deepseek", "auto")

        assert req.extra_body == {"thinking": {"type": "enabled"}}
        assert req.reasoning_effort is None, (
            "auto must let DeepSeek pick its own effort natively"
        )
        assert req.effort_sent is False
        assert req.effort_policy == EFFORT_OMITTED_PROVIDER_AUTO

    def test_auto_request_kwargs_have_no_reasoning_effort_key(self) -> None:
        """Omission means the key is absent — not present-and-null."""
        kwargs = _build_kwargs("deepseek", "auto")

        assert "reasoning_effort" not in kwargs
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    @pytest.mark.parametrize("mode", ["high", "max"])
    def test_explicit_selection_is_sent_verbatim(self, mode: str) -> None:
        req = resolve_reasoning_request("deepseek", mode)

        assert req.reasoning_effort == mode
        assert req.effort_sent is True
        assert req.effort_policy == EFFORT_EXPLICIT
        assert req.extra_body == {"thinking": {"type": "enabled"}}
        assert _build_kwargs("deepseek", mode)["reasoning_effort"] == mode

    def test_off_is_unchanged(self) -> None:
        req = resolve_reasoning_request("deepseek", "off")

        assert req.extra_body == {"thinking": {"type": "disabled"}}
        assert req.reasoning_effort is None
        assert req.send_temperature is True
        assert req.effort_policy == EFFORT_OMITTED_DISABLED

        kwargs = _build_kwargs("deepseek", "off")
        assert "reasoning_effort" not in kwargs
        assert kwargs["temperature"] == 0.7


def _build_kwargs(provider: str, thinking: str, temperature: float = 0.7) -> dict[str, Any]:
    """Reproduce exactly how DeepSeekClient.stream assembles request kwargs."""
    kwargs: dict[str, Any] = {"model": "m", "messages": [], "stream": True}
    req = resolve_reasoning_request(provider, thinking)
    if req.extra_body is not None:
        kwargs["extra_body"] = req.extra_body
    if req.reasoning_effort is not None:
        kwargs["reasoning_effort"] = req.reasoning_effort
    if req.send_temperature:
        kwargs["temperature"] = temperature
    return kwargs


def test_stream_builds_kwargs_the_same_way_as_this_test() -> None:
    """Guard the helper above against drifting from the real client."""
    source = inspect.getsource(
        __import__("aura.client.deepseek", fromlist=["DeepSeekClient"]).DeepSeekClient.stream
    )
    assert "resolve_reasoning_request(self._provider, thinking)" in source
    assert "if reasoning.reasoning_effort is not None:" in source
    assert 'kwargs["reasoning_effort"] = reasoning.reasoning_effort' in source


# ── other providers ─────────────────────────────────────────────────────────


class TestOtherProviders:

    @pytest.mark.parametrize("provider", ["openai", "openrouter"])
    def test_auto_omits_effort_so_the_provider_default_applies(
        self, provider: str
    ) -> None:
        req = resolve_reasoning_request(provider, "auto")

        assert req.reasoning_effort is None
        assert req.effort_policy == EFFORT_OMITTED_PROVIDER_DEFAULT
        assert "reasoning_effort" not in _build_kwargs(provider, "auto")

    @pytest.mark.parametrize("provider", ["openai", "openrouter"])
    @pytest.mark.parametrize("mode", ["high", "max"])
    def test_explicit_selection_still_sent(self, provider: str, mode: str) -> None:
        assert _build_kwargs(provider, mode)["reasoning_effort"] == mode

    def test_anthropic_auto_uses_native_adaptive_without_an_effort_override(
        self,
    ) -> None:
        config = _anthropic_thinking_config("claude-sonnet-4-6", "auto")

        assert config["thinking"]["type"] == "adaptive"
        assert "output_config" not in config, (
            "auto must not override adaptive thinking's own effort choice"
        )
        assert (
            _anthropic_effort_policy("claude-sonnet-4-6", "auto")
            == EFFORT_OMITTED_PROVIDER_AUTO
        )

    @pytest.mark.parametrize("mode", ["high", "max"])
    def test_anthropic_explicit_selection_is_stated(self, mode: str) -> None:
        config = _anthropic_thinking_config("claude-sonnet-4-6", mode)

        assert config["output_config"] == {"effort": mode}
        assert _anthropic_effort_policy("claude-sonnet-4-6", mode) == EFFORT_EXPLICIT

    def test_provider_without_native_auto_keeps_its_deterministic_default(self) -> None:
        """A budget-only model has no auto mode, so auto uses existing behaviour."""
        auto = _anthropic_thinking_config("claude-haiku-4-5", "auto")
        high = _anthropic_thinking_config("claude-haiku-4-5", "high")

        assert auto == high, "auto must not invent a new budget for this model"
        assert (
            _anthropic_effort_policy("claude-haiku-4-5", "auto")
            == EFFORT_OMITTED_PROVIDER_DEFAULT
        )

    def test_gemini_auto_uses_documented_dynamic_thinking_budget(self) -> None:
        from aura.providers.google_cloud.client import _google_thinking_config

        class _Types:
            class ThinkingConfig:
                def __init__(self, thinking_budget: int, include_thoughts: bool) -> None:
                    self.thinking_budget = thinking_budget
                    self.include_thoughts = include_thoughts

        config = _google_thinking_config(_Types, "gemini-2.5-pro", "auto")

        assert config.thinking_budget == -1, (
            "Gemini's documented dynamic thinking is budget -1"
        )


# ── no Aura-side complexity routing ─────────────────────────────────────────


class TestNoComplexityRouter:
    """``auto`` must delegate, never estimate."""

    def test_resolution_depends_only_on_provider_and_selected_mode(self) -> None:
        signature = inspect.signature(resolve_reasoning_request)
        assert list(signature.parameters) == ["provider", "thinking"], (
            "resolution must not accept task text, history, or failure counts"
        )

    def test_auto_never_resolves_to_high_or_max(self) -> None:
        for provider in ("deepseek", "openai", "openrouter"):
            req = resolve_reasoning_request(provider, "auto")
            assert req.reasoning_effort not in {"high", "max"}
            assert req.reasoning_effort is None

    def test_auto_is_stable_across_repeated_calls(self) -> None:
        """No hidden counter escalates a repeated auto request."""
        first = resolve_reasoning_request("deepseek", "auto")
        rest = [resolve_reasoning_request("deepseek", "auto") for _ in range(25)]

        assert all(r == first for r in rest)

    def test_unknown_mode_falls_back_to_auto_not_max(self) -> None:
        req = resolve_reasoning_request("deepseek", "something-new")

        assert req.thinking == "auto"
        assert req.reasoning_effort is None, "never silently promote to max"

    def test_resolution_is_order_independent(self) -> None:
        """Interleaved modes cannot influence one another — there is no state."""
        isolated = {mode: resolve_reasoning_request("deepseek", mode) for mode in ALL_MODES}

        sequence = ["auto", "high", "auto", "max", "auto", "off", "auto"]
        for mode in sequence:
            assert resolve_reasoning_request("deepseek", mode) == isolated[mode]

    def test_module_holds_no_mutable_state_to_escalate_with(self) -> None:
        """No counters, ledgers, or caches that could ratchet effort upward."""
        mutable = {
            name: value
            for name, value in vars(reasoning_module).items()
            if not name.startswith("__")
            and isinstance(value, (list, dict, set, bytearray))
        }
        assert mutable == {}, f"mutable module state could accumulate: {mutable}"

    def test_resolver_reads_nothing_but_its_arguments(self) -> None:
        """The function body references no global lookup tables or clocks."""
        code = resolve_reasoning_request.__code__
        assert set(code.co_names) <= {
            "ReasoningRequest",
            "EFFORT_EXPLICIT",
            "EFFORT_OMITTED_DISABLED",
            "EFFORT_OMITTED_PROVIDER_AUTO",
            "EFFORT_OMITTED_PROVIDER_DEFAULT",
            "_explicit_effort",
        }, f"unexpected global reference: {code.co_names}"


# ── persistence round-trips, and never migrates an explicit choice ──────────


class TestSettingsPersistence:

    def test_auto_round_trips_through_settings(self) -> None:
        loaded = AppSettings.from_dict({"default_thinking": "auto"})
        assert loaded.default_thinking == "auto"

    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_every_mode_round_trips(self, mode: str) -> None:
        assert AppSettings.from_dict({"default_thinking": mode}).default_thinking == mode

    @pytest.mark.parametrize("mode", ["off", "high", "max"])
    def test_existing_explicit_selection_is_never_migrated_to_auto(
        self, mode: str
    ) -> None:
        loaded = AppSettings.from_dict(
            {"provider": "deepseek", "default_model": "deepseek-v4-flash",
             "default_thinking": mode}
        )
        assert loaded.default_thinking == mode, (
            "an explicit saved selection must survive verbatim"
        )

    def test_legacy_planner_thinking_migrates_to_its_own_value_not_auto(self) -> None:
        loaded = AppSettings.from_dict({"default_planner_thinking": "max"})
        assert loaded.default_thinking == "max"

    def test_unset_deepseek_settings_default_to_auto(self) -> None:
        loaded = AppSettings.from_dict({"provider": "deepseek"})
        assert loaded.default_thinking == "auto"

    def test_legacy_worker_thinking_values_still_load(self) -> None:
        loaded = AppSettings.from_dict({"default_worker_thinking": "high"})
        assert loaded.default_worker_thinking == "high"


class TestConversationPersistence:

    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_thinking_round_trips_through_a_saved_conversation(
        self, mode: str, tmp_path
    ) -> None:
        from aura.conversation.history import History
        from aura.conversation.persistence import load_conversation, save_conversation

        history = History()
        history.set_system("system")
        history.append_user_text("hello")

        path = save_conversation(
            history, tmp_path, model="deepseek-v4-flash", thinking=mode
        )
        loaded = load_conversation(path)

        assert loaded.thinking == mode

    def test_a_saved_auto_conversation_records_auto_on_disk(self, tmp_path) -> None:
        from aura.conversation.history import History
        from aura.conversation.persistence import save_conversation

        history = History()
        history.set_system("system")
        history.append_user_text("hello")

        path = save_conversation(
            history, tmp_path, model="deepseek-v4-flash", thinking="auto"
        )

        assert json.loads(path.read_text(encoding="utf-8"))["thinking"] == "auto"

    @pytest.mark.parametrize("mode", ["off", "high", "max"])
    def test_an_older_record_keeps_its_explicit_mode(self, mode: str, tmp_path) -> None:
        from aura.conversation.history import History
        from aura.conversation.persistence import load_conversation, save_conversation

        history = History()
        history.set_system("system")
        history.append_user_text("hello")
        path = save_conversation(
            history, tmp_path, model="deepseek-v4-flash", thinking=mode
        )

        assert load_conversation(path).thinking == mode
