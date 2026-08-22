"""Focused tests for the DeepSeek pricing source boundary.

Covers the required behaviors: semantic parsing of the official pricing-page
structure (local HTML fixtures), independence from model column order,
incomplete-page rejection, last-known-good cache fallback, UTC peak/off-peak
tier selection, and the existing cost path (``cost_usd``) receiving fetched
DeepSeek rates. Every test uses local fixtures — no network access.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aura.config import cost_usd, get_pricing
from aura.providers import pricing
from aura.providers.pricing import (
    PeakWindow,
    RateTier,
    ScheduleOverride,
    is_stale,
    load_pricing_cache,
    priced_lookup_for,
    pricing_cache_path,
    rates_for,
    refresh_provider_pricing,
)
from aura.providers.pricing_sources.deepseek import DeepSeekPricingSource

FIXTURES = Path(__file__).parent / "fixtures" / "deepseek_pricing"

OFF_PEAK_UTC = datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc)  # between the peak windows
PEAK_UTC = datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc)  # inside 01:00-04:00 UTC


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def pricing_store_clean():
    """Snapshot and restore the global pricing store around every test."""
    saved = dict(pricing._results)
    pricing._results.clear()
    yield
    pricing._results.clear()
    pricing._results.update(saved)


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """Point the pricing cache at a per-test directory."""
    monkeypatch.setenv("AURA_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _standard_result() -> pricing.PricingResult:
    result = DeepSeekPricingSource().parse(_fixture("pricing_standard.html"))
    assert result is not None
    return result


def _seed_standard() -> None:
    pricing._results["deepseek"] = _standard_result()


# ---------------------------------------------------------------------------
# Semantic parsing
# ---------------------------------------------------------------------------


class TestDeepSeekPricingParse:
    def test_parses_standard_page(self) -> None:
        result = DeepSeekPricingSource().parse(_fixture("pricing_standard.html"))
        assert result is not None
        assert result.provider_id == "deepseek"
        assert result.source_url == "https://api-docs.deepseek.com/quick_start/pricing/"
        assert result.retrieved_at
        assert set(result.models) == {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        }

        flash = result.models["deepseek-v4-flash"]
        assert flash.off_peak == RateTier(in_miss=0.22, in_hit=0.007, out=0.66)
        assert flash.peak == RateTier(in_miss=0.44, in_hit=0.014, out=1.32)

        pro = result.models["deepseek-v4-pro"]
        assert pro.off_peak == RateTier(in_miss=0.66, in_hit=0.022, out=1.98)
        assert pro.peak == RateTier(in_miss=1.32, in_hit=0.044, out=3.96)

        vision = result.models["deepseek-v4-flash-vision-exp"]
        assert vision.off_peak == RateTier(in_miss=0.22, in_hit=0.007, out=0.66)

        # The published peak/off-peak UTC schedule is parsed from the page.
        assert result.peak_windows == (PeakWindow(start_minute=60, end_minute=240), PeakWindow(start_minute=360, end_minute=600))

        # The published effective-dated Beijing-Time weekend rule is parsed too.
        assert result.schedule_overrides == (
            ScheduleOverride(
                effective_at="2026-08-22T16:00:00+00:00",
                utc_offset_minutes=480,
                weekdays=(5, 6),
                tier="off_peak",
            ),
        )

    def test_numeric_non_pricing_rows_are_not_read_as_rates(self) -> None:
        """The concurrency-limits row (2500/500/2500) must not become rates."""
        result = _standard_result()
        assert result.models["deepseek-v4-flash"].off_peak.out == 0.66
        assert result.models["deepseek-v4-flash"].off_peak.in_miss == 0.22

    def test_changed_column_order_still_maps_by_model_header(self) -> None:
        result = DeepSeekPricingSource().parse(_fixture("pricing_reordered_columns.html"))
        assert result is not None
        assert set(result.models) == {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        }
        # Each model keeps its own column's rates regardless of order.
        assert result.models["deepseek-v4-flash"].off_peak == RateTier(in_miss=0.22, in_hit=0.007, out=0.66)
        assert result.models["deepseek-v4-pro"].off_peak == RateTier(in_miss=0.66, in_hit=0.022, out=1.98)
        assert result.models["deepseek-v4-flash-vision-exp"].off_peak == RateTier(in_miss=0.22, in_hit=0.007, out=0.66)

    def test_incomplete_page_rejected(self) -> None:
        result = DeepSeekPricingSource().parse(_fixture("pricing_incomplete.html"))
        assert result is None

    def test_page_without_pricing_table_rejected(self) -> None:
        assert DeepSeekPricingSource().parse("<html><body><p>nothing here</p></body></html>") is None

    def test_garbage_html_rejected(self) -> None:
        assert DeepSeekPricingSource().parse("<table><tr><td>MODEL</td></tr></table>") is None


# ---------------------------------------------------------------------------
# Store, validation, and the last-known-good cache
# ---------------------------------------------------------------------------


class TestPricingStoreCache:
    def test_successful_fetch_persists_cache(self, isolated_config_dir) -> None:
        pricing._results["deepseek"] = _standard_result()
        pricing._save_cache(pricing._results["deepseek"])
        path = pricing_cache_path()
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["deepseek"]["models"]["deepseek-v4-flash"]["off_peak"]["in_miss"] == 0.22
        assert data["deepseek"]["peak_windows"] == [[60, 240], [360, 600]]

    def test_network_failure_preserves_previous_valid_cache(self, monkeypatch, isolated_config_dir) -> None:
        valid = _standard_result()
        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: valid)
        first = refresh_provider_pricing("deepseek")
        assert first is not None
        assert first.models["deepseek-v4-flash"].off_peak.in_miss == 0.22

        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: None)  # network failure
        kept = refresh_provider_pricing("deepseek")
        assert kept is not None
        assert kept is first
        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC)["in_miss"] == 0.22

    def test_incomplete_fetch_preserves_previous_valid_cache(self, monkeypatch, isolated_config_dir) -> None:
        valid = _standard_result()
        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: valid)
        refresh_provider_pricing("deepseek")
        # A malformed page parses to None — the cache must survive.
        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: None)
        kept = refresh_provider_pricing("deepseek")
        assert kept is not None

    def test_failed_fetch_falls_back_to_disk_cache_when_store_empty(self, monkeypatch, isolated_config_dir) -> None:
        # A last-known-good cache exists on disk, the store is empty
        # (fresh process), and the network is down.
        pricing._results["deepseek"] = _standard_result()
        pricing._save_cache(pricing._results["deepseek"])
        pricing._results.clear()

        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: None)
        result = refresh_provider_pricing("deepseek")
        assert result is not None
        assert result.models["deepseek-v4-flash"].off_peak.in_miss == 0.22
        assert result.peak_windows == (PeakWindow(start_minute=60, end_minute=240), PeakWindow(start_minute=360, end_minute=600))

    def test_corrupt_cache_file_ignored(self, isolated_config_dir) -> None:
        pricing_cache_path().write_text("{not json", encoding="utf-8")
        load_pricing_cache()  # must not raise
        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) is None

    def test_startup_loads_cached_rates(self, isolated_config_dir) -> None:
        pricing._results["deepseek"] = _standard_result()
        pricing._save_cache(pricing._results["deepseek"])
        pricing._results.clear()  # simulate a fresh process

        load_pricing_cache()
        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) == {
            "in_miss": 0.22,
            "in_hit": 0.007,
            "out": 0.66,
        }

    def test_invalid_cached_entry_not_loaded(self, isolated_config_dir) -> None:
        pricing._results["deepseek"] = _standard_result()
        pricing._save_cache(pricing._results["deepseek"])
        # Corrupt one model's rates on disk so the entry fails validation.
        data = json.loads(pricing_cache_path().read_text(encoding="utf-8"))
        data["deepseek"]["models"]["deepseek-v4-flash"]["off_peak"]["out"] = -1.0
        pricing_cache_path().write_text(json.dumps(data), encoding="utf-8")
        pricing._results.clear()

        load_pricing_cache()
        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) is None

    def test_schedule_override_round_trips_through_cache(self, isolated_config_dir) -> None:
        pricing._results["deepseek"] = _standard_result()
        pricing._save_cache(pricing._results["deepseek"])
        pricing._results.clear()  # simulate a fresh process

        load_pricing_cache()
        result = pricing.get_result("deepseek")
        assert result is not None
        assert result.schedule_overrides == (
            ScheduleOverride(
                effective_at="2026-08-22T16:00:00+00:00",
                utc_offset_minutes=480,
                weekdays=(5, 6),
                tier="off_peak",
            ),
        )

    def test_old_cache_schema_without_schedule_overrides_is_rejected(self, isolated_config_dir) -> None:
        """The pre-schedule-override cache shape (no schema_version field) must be
        rejected outright rather than silently loaded without its weekly rule."""
        legacy = {
            "deepseek": {
                "provider_id": "deepseek",
                "source_url": "https://api-docs.deepseek.com/quick_start/pricing/",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
                "peak_windows": [[60, 240], [360, 600]],
                "models": {
                    "deepseek-v4-flash": {
                        "model_id": "deepseek-v4-flash",
                        "off_peak": {"in_miss": 0.22, "in_hit": 0.007, "out": 0.66},
                        "peak": {"in_miss": 0.44, "in_hit": 0.014, "out": 1.32},
                    }
                },
            }
        }
        pricing_cache_path().write_text(json.dumps(legacy), encoding="utf-8")

        load_pricing_cache()
        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) is None


# ---------------------------------------------------------------------------
# UTC peak/off-peak tier selection
# ---------------------------------------------------------------------------


class TestUtcTierSelection:
    def test_peak_window_selection(self) -> None:
        _seed_standard()
        assert rates_for("deepseek", "deepseek-v4-flash", PEAK_UTC) == {
            "in_miss": 0.44,
            "in_hit": 0.014,
            "out": 1.32,
        }
        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) == {
            "in_miss": 0.22,
            "in_hit": 0.007,
            "out": 0.66,
        }

    def test_window_boundaries(self) -> None:
        _seed_standard()
        # 01:00 UTC exactly is inside the first peak window (start inclusive).
        assert rates_for("deepseek", "deepseek-v4-flash", datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc))["in_miss"] == 0.44
        # 04:00 UTC exactly is past the first window (end exclusive).
        assert rates_for("deepseek", "deepseek-v4-flash", datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc))["in_miss"] == 0.22
        # 06:00 UTC exactly opens the second window.
        assert rates_for("deepseek", "deepseek-v4-flash", datetime(2025, 1, 1, 6, 0, tzinfo=timezone.utc))["in_miss"] == 0.44
        # 10:00 UTC exactly closes it.
        assert rates_for("deepseek", "deepseek-v4-flash", datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc))["in_miss"] == 0.22

    def test_unknown_model_returns_none(self) -> None:
        _seed_standard()
        assert rates_for("deepseek", "nonexistent-model", OFF_PEAK_UTC) is None


# ---------------------------------------------------------------------------
# Beijing-Time weekend override — effective-dated all-day weekly tier rule
# ---------------------------------------------------------------------------


class TestDeepSeekWeekendSchedule:
    # 00:00 Beijing Time on Sunday, August 23, 2026 == 2026-08-22T16:00:00Z.
    PRE_EFFECTIVE_SATURDAY_PEAK_UTC = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)  # Beijing Sat, before effective instant
    POST_EFFECTIVE_SATURDAY_PEAK_UTC = datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc)  # Beijing Sat Aug 29, in a UTC peak window
    POST_EFFECTIVE_SUNDAY_PEAK_UTC = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)  # Beijing Sun Aug 30, in a UTC peak window
    POST_EFFECTIVE_WEEKDAY_PEAK_UTC = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)  # Beijing Mon Aug 31, in a UTC peak window

    def test_pre_effective_saturday_still_selects_daily_peak_window(self) -> None:
        """Before the effective instant, the weekend override does not apply yet —
        the original recurring daily UTC peak window still governs."""
        _seed_standard()
        assert rates_for("deepseek", "deepseek-v4-flash", self.PRE_EFFECTIVE_SATURDAY_PEAK_UTC)["in_miss"] == 0.44

    def test_post_effective_beijing_saturday_and_sunday_select_off_peak(self) -> None:
        """On or after the effective instant, Beijing Saturday and Sunday are
        entirely off-peak even inside a published UTC peak window."""
        _seed_standard()
        assert rates_for("deepseek", "deepseek-v4-flash", self.POST_EFFECTIVE_SATURDAY_PEAK_UTC)["in_miss"] == 0.22
        assert rates_for("deepseek", "deepseek-v4-flash", self.POST_EFFECTIVE_SUNDAY_PEAK_UTC)["in_miss"] == 0.22

    def test_post_effective_weekday_still_uses_published_peak_window(self) -> None:
        """A Beijing weekday keeps using the recurring UTC peak windows, even
        after the weekend override has taken effect."""
        _seed_standard()
        assert rates_for("deepseek", "deepseek-v4-flash", self.POST_EFFECTIVE_WEEKDAY_PEAK_UTC)["in_miss"] == 0.44

    def test_weekend_rule_advertised_but_incomplete_is_rejected(self) -> None:
        """A page that mentions Beijing Time but doesn't fully spell out the
        effective date and weekday/tier rule must be rejected outright — the
        rule is never silently dropped."""
        result = DeepSeekPricingSource().parse(_fixture("pricing_weekend_rule_incomplete.html"))
        assert result is None


# ---------------------------------------------------------------------------
# Existing cost path receiving fetched DeepSeek rates
# ---------------------------------------------------------------------------


class TestCostPathWithFetchedRates:
    def test_cost_usd_uses_fetched_rates(self, monkeypatch) -> None:
        monkeypatch.setattr(pricing, "_utcnow", lambda: OFF_PEAK_UTC)
        _seed_standard()

        cost = cost_usd("deepseek-v4-flash", 500, 1000, 800)
        assert cost is not None
        expected = (500 * 0.007 + 1000 * 0.22 + 800 * 0.66) / 1_000_000
        assert abs(cost - expected) < 1e-12

    def test_cost_usd_applies_peak_tier_by_utc_time(self, monkeypatch) -> None:
        monkeypatch.setattr(pricing, "_utcnow", lambda: PEAK_UTC)
        _seed_standard()

        cost = cost_usd("deepseek-v4-flash", 500, 1000, 800)
        assert cost is not None
        expected = (500 * 0.014 + 1000 * 0.44 + 800 * 1.32) / 1_000_000
        assert abs(cost - expected) < 1e-12

    def test_get_pricing_returns_fetched_deepseek_rates(self, monkeypatch) -> None:
        monkeypatch.setattr(pricing, "_utcnow", lambda: OFF_PEAK_UTC)
        _seed_standard()

        assert get_pricing("deepseek-v4-pro") == {"in_miss": 0.66, "in_hit": 0.022, "out": 1.98}

    def test_unavailable_without_fetched_or_cached_prices(self) -> None:
        # No store, no cache: the existing unavailable-price behavior (None)
        # applies — never a fabricated fallback.
        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) is None
        assert get_pricing("deepseek-v4-flash") is None
        assert cost_usd("deepseek-v4-flash", 0, 0, 0) is None

    def test_other_providers_unchanged(self) -> None:
        from aura.providers import catalog as catalog_module
        from aura.providers.pricing import has_pricing_source

        # Only DeepSeek participates in the pricing-source boundary.
        assert has_pricing_source("deepseek")
        for pid in ("openrouter", "openai", "anthropic", "google_cloud"):
            assert not has_pricing_source(pid)

        source = Path(inspect.getsourcefile(catalog_module)).read_text(encoding="utf-8")
        # No other provider's catalog pricing was touched by this change.
        assert '"deepseek/deepseek-v4-flash": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60}' in source
        assert '"gpt-5.4-mini": {"in_miss": 0.15, "in_hit": 0.075, "out": 0.60}' in source
        assert '"gemini-2.5-flash": {"in_miss": 0.075, "in_hit": 0.01875, "out": 0.30}' in source
        assert '"claude-sonnet-4-6": {"in_miss": 3.00, "in_hit": 0.30, "out": 15.00}' in source

        # Catalog-backed providers still resolve through get_pricing.
        assert get_pricing("gpt-5.4-mini") is not None
        assert get_pricing("gemini-2.5-flash") is not None

    def test_runtime_cost_path_ignores_stale_catalog_rates(self) -> None:
        """A stale dynamic models cache may inject rates into the shared
        catalog dicts (see load_dynamic_catalog), but the cost path for a
        sourced provider consults only the pricing store: with no fetched or
        cached result, deepseek models are unavailable — never a catalog
        fallback."""
        assert get_pricing("deepseek-v4-flash") is None
        assert cost_usd("deepseek-v4-flash", 0, 0, 0) is None

    def test_catalog_source_holds_no_hardcoded_deepseek_rates(self) -> None:
        from aura.providers import catalog as catalog_module

        source = Path(inspect.getsourcefile(catalog_module)).read_text(encoding="utf-8")
        assert "DEEPSEEK_PRICING" not in source
        # The old hardcoded DeepSeek rate literals are gone from the catalog.
        for literal in ("0.0028", "0.003625", "0.435"):
            assert literal not in source


# ---------------------------------------------------------------------------
# priced_lookup_for — the Decimal-costing entry point for per-event telemetry
# ---------------------------------------------------------------------------


class TestPricedLookupFor:
    def test_returns_tier_and_provenance_off_peak(self) -> None:
        _seed_standard()
        lookup = priced_lookup_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC)
        assert lookup is not None
        assert lookup.tier == "off_peak"
        assert lookup.rates == {"in_miss": 0.22, "in_hit": 0.007, "out": 0.66}
        assert lookup.source_url == "https://api-docs.deepseek.com/quick_start/pricing/"
        assert lookup.retrieved_at
        assert lookup.stale is False

    def test_returns_peak_tier_inside_window(self) -> None:
        _seed_standard()
        lookup = priced_lookup_for("deepseek", "deepseek-v4-flash", PEAK_UTC)
        assert lookup is not None
        assert lookup.tier == "peak"
        assert lookup.rates == {"in_miss": 0.44, "in_hit": 0.014, "out": 1.32}

    def test_none_when_model_or_provider_unpriced(self) -> None:
        _seed_standard()
        assert priced_lookup_for("deepseek", "nonexistent-model", OFF_PEAK_UTC) is None
        assert priced_lookup_for("openai", "gpt-5.4-mini", OFF_PEAK_UTC) is None


class TestIsStale:
    def test_fresh_result_is_not_stale(self) -> None:
        result = _standard_result()
        now = datetime.fromisoformat(result.retrieved_at) + timedelta(hours=1)
        assert is_stale(result, now) is False

    def test_result_older_than_seven_days_is_stale(self) -> None:
        result = _standard_result()
        retrieved = datetime.fromisoformat(result.retrieved_at)
        now = retrieved + timedelta(days=8)
        assert is_stale(result, now) is True

    def test_unparsable_retrieved_at_is_treated_as_stale(self) -> None:
        from dataclasses import replace

        result = replace(_standard_result(), retrieved_at="not-a-timestamp")
        assert is_stale(result) is True


# ---------------------------------------------------------------------------
# End-to-end: ConversationTelemetry prices each event from the DeepSeek
# pricing store at the moment usage was observed.
# ---------------------------------------------------------------------------


class TestConversationTelemetryPricesDeepSeekEvents:
    def test_record_usage_prices_from_the_active_tier_and_never_reprices(self, monkeypatch) -> None:
        from decimal import Decimal

        from aura.conversation.telemetry import ConversationTelemetry

        _seed_standard()
        telemetry = ConversationTelemetry()
        telemetry.record_usage(
            model_id="deepseek-v4-flash",
            prompt=1_500,
            completion=800,
            hit=500,
            miss=1_000,
            context_window_tokens=1_000_000,
            now=OFF_PEAK_UTC,
        )
        event = telemetry.events[0]
        assert event.provider_id == "deepseek"
        assert event.pricing_tier == "off_peak"
        expected = (Decimal(500) * Decimal("0.007") + Decimal(1000) * Decimal("0.22") + Decimal(800) * Decimal("0.66")) / Decimal(1_000_000)
        assert event.cost_decimal() == expected
        assert event.source_url == "https://api-docs.deepseek.com/quick_start/pricing/"

        # The tier flips to peak for a second call — the first event's
        # already-persisted cost must not move.
        telemetry.record_usage(
            model_id="deepseek-v4-flash",
            prompt=1_500,
            completion=800,
            hit=500,
            miss=1_000,
            context_window_tokens=1_000_000,
            now=PEAK_UTC,
        )
        assert telemetry.events[0].cost_decimal() == expected  # unchanged
        assert telemetry.events[1].pricing_tier == "peak"
        assert telemetry.events[1].cost_decimal() != expected

    def test_stale_pricing_result_is_flagged_on_the_event(self) -> None:
        from dataclasses import replace

        stale_result = replace(
            _standard_result(),
            retrieved_at=(OFF_PEAK_UTC - timedelta(days=30)).isoformat(),
        )
        pricing._results["deepseek"] = stale_result

        from aura.conversation.telemetry import ConversationTelemetry

        telemetry = ConversationTelemetry()
        telemetry.record_usage(
            model_id="deepseek-v4-flash",
            prompt=100,
            completion=10,
            hit=0,
            miss=100,
            context_window_tokens=1_000_000,
            now=OFF_PEAK_UTC,
        )
        assert telemetry.events[0].stale is True

    def test_weekend_override_prices_event_as_off_peak(self) -> None:
        """A usage event observed on a post-effective Beijing weekend, inside a
        published UTC peak window, must be priced off-peak and persist that
        tier — the weekend override outranks the daily peak window."""
        from decimal import Decimal

        from aura.conversation.telemetry import ConversationTelemetry

        _seed_standard()
        post_effective_saturday_peak = datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc)
        telemetry = ConversationTelemetry()
        telemetry.record_usage(
            model_id="deepseek-v4-flash",
            prompt=1_500,
            completion=800,
            hit=500,
            miss=1_000,
            context_window_tokens=1_000_000,
            now=post_effective_saturday_peak,
        )
        event = telemetry.events[0]
        assert event.pricing_tier == "off_peak"
        expected = (Decimal(500) * Decimal("0.007") + Decimal(1000) * Decimal("0.22") + Decimal(800) * Decimal("0.66")) / Decimal(1_000_000)
        assert event.cost_decimal() == expected

    def test_missing_pricing_result_records_unknown_cost(self) -> None:
        from aura.conversation.telemetry import ConversationTelemetry

        telemetry = ConversationTelemetry()
        telemetry.record_usage(
            model_id="deepseek-v4-flash",
            prompt=100,
            completion=10,
            hit=0,
            miss=100,
            context_window_tokens=1_000_000,
            now=OFF_PEAK_UTC,
        )
        event = telemetry.events[0]
        assert event.pricing_tier == "unknown"
        assert event.cost is None
        assert event.cost_decimal() is None