"""DeepSeek model capacity and Aura's spending policy are separate things.

The catalog states what a model can physically hold. The working-set cap states
what Aura is willing to spend on an ordinary coding request. Conflating them —
encoding a fake window to control cost, or letting a corrected window silently
multiply spend — is the failure mode these tests exist to prevent.

What is asserted here:

* both DeepSeek models advertise ``1_000_000 / 384_000``;
* the explicit product cap keeps the effective working set near ``72_000``;
* capacity and policy are reported as distinct values, not one blended number;
* a stale on-disk model cache cannot silently downgrade corrected capacity;
* providers without a cap are unaffected.
"""

from __future__ import annotations

import json

import pytest

from aura.config import (
    DEEPSEEK_WORKING_SET_CAP_TOKENS,
    PROVIDER_WORKING_SET_CAP_TOKENS,
)
from aura.conversation.context_budget import resolve_model_budget
from aura.providers.registry import provider_registry

DEEPSEEK_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")


# ── capacity: what the model can hold ───────────────────────────────────────


class TestAdvertisedCapacity:

    @pytest.mark.parametrize("model_id", DEEPSEEK_MODELS)
    def test_catalog_advertises_the_real_window_and_output(self, model_id: str) -> None:
        info = provider_registry.get("deepseek").models[model_id]

        assert info.context_window_tokens == 1_000_000
        assert info.max_output_tokens == 384_000

    @pytest.mark.parametrize("model_id", DEEPSEEK_MODELS)
    def test_resolved_budget_reports_the_real_window(self, model_id: str) -> None:
        budget = resolve_model_budget(model_id)

        assert budget.context_window_tokens == 1_000_000
        assert budget.is_fallback is False
        assert budget.provider_id == "deepseek"

    @pytest.mark.parametrize("model_id", DEEPSEEK_MODELS)
    def test_no_fake_window_is_encoded_to_control_spending(self, model_id: str) -> None:
        """The window is capacity metadata; it must never be the cost lever."""
        info = provider_registry.get("deepseek").models[model_id]

        assert info.context_window_tokens != DEEPSEEK_WORKING_SET_CAP_TOKENS
        assert info.context_window_tokens > DEEPSEEK_WORKING_SET_CAP_TOKENS


# ── policy: what Aura will spend ────────────────────────────────────────────


class TestSpendingPolicy:

    def test_the_deepseek_cap_is_an_explicit_named_constant(self) -> None:
        assert DEEPSEEK_WORKING_SET_CAP_TOKENS == 72_000
        assert PROVIDER_WORKING_SET_CAP_TOKENS["deepseek"] == 72_000

    @pytest.mark.parametrize("model_id", DEEPSEEK_MODELS)
    def test_effective_working_set_stays_near_the_practical_budget(
        self, model_id: str
    ) -> None:
        budget = resolve_model_budget(model_id)

        assert budget.working_set_tokens == 72_000
        # The pre-correction practical budget was ~71_884 tokens; correcting the
        # metadata must not have moved ordinary coding requests off it.
        assert abs(budget.working_set_tokens - 71_884) < 1_000

    @pytest.mark.parametrize("model_id", DEEPSEEK_MODELS)
    def test_correcting_capacity_did_not_expand_ordinary_requests(
        self, model_id: str
    ) -> None:
        budget = resolve_model_budget(model_id)

        assert budget.derived_working_set_tokens > 300_000, (
            "the uncapped model-derived budget really is huge"
        )
        assert budget.working_set_tokens < 100_000, (
            "the product cap is what keeps a normal turn affordable"
        )

    @pytest.mark.parametrize("model_id", DEEPSEEK_MODELS)
    def test_the_cap_is_the_binding_constraint_and_says_so(self, model_id: str) -> None:
        budget = resolve_model_budget(model_id)

        assert budget.capped_by_policy is True
        assert budget.working_set_tokens == min(
            budget.derived_working_set_tokens, budget.policy_cap_tokens
        )

    def test_other_providers_are_untouched_by_the_deepseek_cap(self) -> None:
        for model_id in ("gemini-2.5-pro", "claude-sonnet-4-6", "gpt-5.4"):
            budget = resolve_model_budget(model_id)

            assert budget.policy_cap_tokens is None, f"{model_id} gained a cap"
            assert budget.capped_by_policy is False
            assert budget.working_set_tokens == budget.derived_working_set_tokens

    def test_an_unknown_model_is_uncapped_and_still_conservative(self) -> None:
        budget = resolve_model_budget("vendor/not-a-real-model")

        assert budget.is_fallback is True
        assert budget.policy_cap_tokens is None
        assert budget.working_set_tokens == budget.derived_working_set_tokens


# ── the two are reported separately ─────────────────────────────────────────


class TestCapacityAndPolicyAreReportedSeparately:

    def test_budget_exposes_capacity_and_policy_as_distinct_fields(self) -> None:
        budget = resolve_model_budget("deepseek-v4-flash")

        # Capacity.
        assert budget.context_window_tokens == 1_000_000
        assert budget.output_reserve_tokens == 384_000
        assert budget.derived_working_set_tokens == 369_600
        # Policy.
        assert budget.policy_cap_tokens == 72_000
        # Outcome.
        assert budget.working_set_tokens == 72_000

    def test_describe_names_window_reserve_derived_cap_and_effective(self) -> None:
        described = resolve_model_budget("deepseek-v4-flash").describe()

        for fragment in (
            "window=1000000",
            "reserve=384000",
            "derived=369600",
            "policy_cap=72000",
            "working_set=72000",
            "capped_by_policy=True",
        ):
            assert fragment in described, f"{fragment!r} missing from {described!r}"

    def test_the_round_log_reports_both(self, caplog) -> None:
        from aura.conversation.manager import _log_context_round

        budget = resolve_model_budget("deepseek-v4-flash")
        stats = _FakeStats()
        stats.tokens_after = 12_000
        stats.reasoning_chars_replayed = 3_000
        stats.reasoning_chars_dropped = 1_500
        tool_defs = [{"type": "function", "function": {"name": "read_file"}}]
        tool_schema_chars = len(json.dumps(tool_defs, ensure_ascii=False))
        tool_schema_tokens = tool_schema_chars // 4
        request_tokens = stats.tokens_after + tool_schema_tokens
        request_headroom = (
            budget.context_window_tokens - budget.output_reserve_tokens - request_tokens
        )

        with caplog.at_level("INFO", logger="aura.conversation.manager"):
            _log_context_round(budget, stats, tool_defs=tool_defs)

        line = caplog.text
        assert "window=1000000" in line
        assert "derived_budget=369600" in line
        assert "policy_cap=72000" in line
        assert "budget=72000" in line
        assert "capped_by_policy=True" in line
        # The same line now also reports the full request shape: tool schema
        # tokens ride outside the working-set budget, and the headroom is what
        # is actually left of the provider window after the whole request.
        assert f"tool_schema_chars={tool_schema_chars}" in line
        assert f"tool_schema_tokens={tool_schema_tokens}" in line
        assert f"request_tokens={request_tokens}" in line
        assert f"request_headroom={request_headroom}" in line
        assert "reasoning_chars_replayed=3000" in line
        assert "reasoning_chars_dropped=1500" in line


class _FakeStats:
    tokens_before = 0
    tokens_after = 0
    messages_before = 0
    messages_after = 0
    system_prompt_chars = 0
    source_result_chars_generated = 0
    source_result_chars_retained = 0
    compacted_results = 0
    dropped_blocks = 0
    repaired_messages = 0
    reasoning_chars_replayed = 0
    reasoning_chars_dropped = 0
    boundary_messages_inserted = 0
    over_budget = False


# ── a stale cache must not undo the correction ──────────────────────────────


class TestStaleCacheCannotDowngradeCapacity:
    """The bug this guards: every existing user has a models_cache.json holding
    a snapshot of the *old* 128K/8K numbers. A provider model listing carries no
    capacity at all, so that snapshot is stale-by-construction — letting it win
    would mean correcting the catalog did nothing for anyone who ever refreshed.
    """

    def test_cached_capacity_does_not_override_a_known_catalog_entry(self) -> None:
        from aura.config import _model_info_from_cache

        known = provider_registry.get("deepseek").models["deepseek-v4-flash"]
        stale = json.loads(json.dumps({
            "id": "deepseek-v4-flash",
            "label": "Deepseek V4 Flash",
            "input_per_m_usd": 0.14,
            "output_per_m_usd": 0.28,
            "cache_hit_per_m_usd": 0.0028,
            "supports_vision": False,
            "context_window_tokens": 128_000,
            "max_output_tokens": 8_192,
        }))

        merged = _model_info_from_cache(stale, known)

        assert merged.context_window_tokens == 1_000_000
        assert merged.max_output_tokens == 384_000

    def test_openrouter_advertised_capacity_is_still_trusted(self) -> None:
        """OpenRouter really does publish context_length; that stays authoritative."""
        from aura.config import _model_info_from_cache

        known = provider_registry.get("openrouter").models[
            "deepseek/deepseek-v4-flash"
        ]
        advertised = {
            "id": "deepseek/deepseek-v4-flash",
            "label": "DeepSeek V4 Flash",
            "input_per_m_usd": 0.15,
            "output_per_m_usd": 0.60,
            "cache_hit_per_m_usd": 0.075,
            "context_window_tokens": 163_840,
            "max_output_tokens": 32_768,
        }

        merged = _model_info_from_cache(advertised, known, trust_cached_capacity=True)

        assert merged.context_window_tokens == 163_840
        assert merged.max_output_tokens == 32_768

    def test_a_cache_missing_capacity_still_inherits_the_catalog(self) -> None:
        from aura.config import _model_info_from_cache

        known = provider_registry.get("deepseek").models["deepseek-v4-pro"]
        legacy = {
            "id": "deepseek-v4-pro",
            "label": "Deepseek V4 Pro",
            "input_per_m_usd": 0.435,
            "output_per_m_usd": 0.87,
            "cache_hit_per_m_usd": 0.003625,
        }

        merged = _model_info_from_cache(legacy, known)

        assert merged.context_window_tokens == 1_000_000
        assert merged.max_output_tokens == 384_000
