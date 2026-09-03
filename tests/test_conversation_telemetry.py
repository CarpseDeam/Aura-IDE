"""Focused coverage for durable conversation footer telemetry."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.conversation.history import History
from aura.conversation.persistence import SCHEMA_VERSION, load_conversation, save_conversation
from aura.conversation.telemetry import (
    ConversationTelemetry,
    UsageEvent,
    context_window_for_model,
)
from aura.gui.conv_persistence import ConversationPersistence
from aura.gui.execution_handler import ExecutionEventHandler
from aura.providers import pricing

_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def pricing_store_clean():
    """These tests assert unpriced/known-pricing states deterministically;
    isolate them from whatever DeepSeek pricing cache happens to exist on
    this machine (aura.config loads it at import time)."""
    saved = dict(pricing._results)
    pricing._results.clear()
    yield
    pricing._results.clear()
    pricing._results.update(saved)


def test_telemetry_round_trips_through_conversation_json(tmp_path) -> None:
    history = History()
    history.append_user_text("hello")
    telemetry = ConversationTelemetry()
    telemetry.record_usage(
        model_id="deepseek-v4-pro",
        prompt=556_000,
        completion=1_413,
        hit=490_240,
        miss=65_880,
        context_window_tokens=1_000_000,
        now=_FIXED_NOW,
    )

    path = save_conversation(history, tmp_path, "deepseek-v4-pro", "off", telemetry=telemetry)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == SCHEMA_VERSION == 3
    assert payload["telemetry"] == {
        "per_model": {"deepseek-v4-pro": {"hit": 490_240, "miss": 65_880, "out": 1_413}},
        "latest_context": {
            "model_id": "deepseek-v4-pro",
            "input_tokens": 556_000,
            "context_window_tokens": 1_000_000,
            "provider_id": "deepseek",
        },
        "events": [
            {
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-pro",
                "timestamp": _FIXED_NOW.isoformat(),
                "cache_hit_tokens": 490_240,
                "cache_miss_tokens": 65_880,
                "output_tokens": 1_413,
                # No DeepSeek pricing has been fetched in this test process,
                # so the sourced provider correctly reports unpriceable.
                "pricing_tier": "unknown",
                "source_url": "",
                "retrieved_at": "",
                "stale": False,
                "exact": False,
                "cost": None,
            }
        ],
    }
    assert load_conversation(path).telemetry.to_dict() == payload["telemetry"]


def test_legacy_conversation_loads_zero_telemetry(tmp_path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"version": 2, "model": "model", "messages": []}), encoding="utf-8")

    assert load_conversation(path).telemetry.to_dict() == {
        "per_model": {},
        "latest_context": {
            "model_id": "",
            "input_tokens": 0,
            "context_window_tokens": 0,
            "provider_id": "",
        },
        "events": [],
    }


def test_record_usage_calculates_decimal_cost_for_catalog_priced_model() -> None:
    telemetry = ConversationTelemetry()
    telemetry.record_usage(
        model_id="gpt-5.4-mini",
        prompt=1_000_000,
        completion=1_000_000,
        hit=0,
        miss=1_000_000,
        context_window_tokens=400_000,
        now=_FIXED_NOW,
    )
    event = telemetry.events[0]
    assert event.provider_id == "openai"
    assert event.pricing_tier == "catalog"
    assert event.cost_decimal() == Decimal("0.15") + Decimal("0.60")
    assert event.exact is False
    assert event.stale is False


def test_local_usage_stays_exactly_free_when_model_id_matches_hosted_catalog() -> None:
    telemetry = ConversationTelemetry()
    telemetry.record_usage(
        provider_id="local_openai",
        model_id="gpt-5.4-mini",
        prompt=1_000_000,
        completion=1_000_000,
        hit=0,
        miss=1_000_000,
        context_window_tokens=0,
        now=_FIXED_NOW,
    )

    event = telemetry.events[0]
    assert event.provider_id == "local_openai"
    assert event.pricing_tier == "local"
    assert event.cost_decimal() == Decimal("0")
    assert event.exact is True
    assert telemetry.latest_context.provider_id == "local_openai"


def test_latest_context_provider_round_trips_and_legacy_payload_defaults_empty() -> None:
    current = ConversationTelemetry()
    current.record_usage(
        provider_id="local_openai",
        model_id="gpt-5.4-mini",
        prompt=12,
        completion=3,
        hit=0,
        miss=12,
        context_window_tokens=0,
        now=_FIXED_NOW,
    )

    restored = ConversationTelemetry.from_dict(current.to_dict())
    assert restored.latest_context.provider_id == "local_openai"

    legacy = ConversationTelemetry.from_dict(
        {
            "latest_context": {
                "model_id": "gpt-5.4-mini",
                "input_tokens": 12,
                "context_window_tokens": 0,
            }
        }
    )
    assert legacy.latest_context.provider_id == ""


def test_provider_qualified_context_lookup_does_not_cross_model_id_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aura.models.PROVIDERS",
        {
            "hosted": SimpleNamespace(
                models={
                    "shared-id": SimpleNamespace(context_window_tokens=128_000)
                }
            ),
            "local_openai": SimpleNamespace(
                models={"shared-id": SimpleNamespace(context_window_tokens=0)}
            ),
        },
    )

    assert context_window_for_model("shared-id", "local_openai") == 0
    assert context_window_for_model("shared-id") == 128_000


def test_cost_summary_has_no_known_total_when_there_are_no_events() -> None:
    """Aggregate-only telemetry with no per-event records is honestly
    unpriceable — no cost is inferred or reconstructed from the totals."""
    telemetry = ConversationTelemetry.from_dict({
        "per_model": {"gpt-5.4-mini": {"hit": 0, "miss": 100, "out": 50}},
        "latest_context": {},
    })
    summary = telemetry.cost_summary()
    assert summary.known_total is None
    assert summary.total_events == 0


def test_cost_summary_sums_only_known_events_and_flags_unknown() -> None:
    telemetry = ConversationTelemetry()
    telemetry.events.append(
        UsageEvent(
            provider_id="openai",
            model_id="gpt-5.4-mini",
            timestamp=_FIXED_NOW.isoformat(),
            cache_hit_tokens=0,
            cache_miss_tokens=1_000_000,
            output_tokens=0,
            pricing_tier="catalog",
            cost="0.15",
        )
    )
    telemetry.events.append(
        UsageEvent(
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            timestamp=_FIXED_NOW.isoformat(),
            cache_hit_tokens=0,
            cache_miss_tokens=100,
            output_tokens=0,
            pricing_tier="unknown",
            cost=None,
        )
    )
    summary = telemetry.cost_summary()
    assert summary.known_total == Decimal("0.15")
    assert summary.unknown_count == 1
    assert summary.total_events == 2


def test_usage_event_round_trips_decimal_cost_string_through_dict() -> None:
    event = UsageEvent(
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        timestamp=_FIXED_NOW.isoformat(),
        cache_hit_tokens=500,
        cache_miss_tokens=1_000,
        output_tokens=800,
        pricing_tier="off_peak",
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        retrieved_at=_FIXED_NOW.isoformat(),
        stale=False,
        exact=False,
        cost="0.001234",
    )
    restored = UsageEvent.from_dict(event.to_dict())
    assert restored == event
    assert restored.cost_decimal() == Decimal("0.001234")


def test_execution_usage_accumulates_but_latest_context_is_replaced() -> None:
    handler = ExecutionEventHandler(
        bridge=object(), chat=object(), playground=object(), settings=object()
    )

    handler._on_execution_usage("run", "deepseek-v4-pro", 100, 10, 40, 60)
    handler._on_execution_usage("run", "deepseek-v4-pro", 250, 20, 0, 0)

    assert handler.conversation_usage == {
        "deepseek-v4-pro": {"hit": 40, "miss": 310, "out": 30}
    }
    assert handler.conversation_telemetry.latest_context.to_dict() == {
        "model_id": "deepseek-v4-pro",
        "input_tokens": 250,
        "context_window_tokens": 1_000_000,
        "provider_id": "deepseek",
    }


class _RestoreBridge:
    def __init__(self) -> None:
        self.history = History()
        self.registry = SimpleNamespace(workspace_root=None)

    def set_temperature(self, _value) -> None: pass
    def set_production_provider(self, _value) -> None: pass
    def refresh_production_prompt(self) -> None: pass
    def clear_pre_execution_snapshot(self) -> None: pass
    def reset_history(self) -> None: self.history = History()


def test_apply_loaded_restores_telemetry_before_status_refresh(tmp_path: Path) -> None:
    restored: list[dict] = []
    bridge = _RestoreBridge()
    persistence = ConversationPersistence(
        bridge=bridge,
        chat=SimpleNamespace(reset=lambda: None),
        playground=SimpleNamespace(clear=lambda: None),
        input_panel=object(),
        left_pane=SimpleNamespace(
            populate_models=lambda _provider: None,
            set_production_model=lambda _model: None,
            set_production_thinking=lambda _thinking: None,
        ),
        settings=SimpleNamespace(temperature=0.7, provider="deepseek"),
        get_conversation_telemetry=ConversationTelemetry,
        restore_conversation_telemetry=lambda telemetry: restored.append(telemetry.to_dict()),
        reset_conversation_usage=lambda: None,
    )
    persistence._render_chat_items = lambda _items: None
    history = History()
    telemetry = ConversationTelemetry.from_dict({
        "per_model": {"model": {"hit": 1, "miss": 2, "out": 3}},
        "latest_context": {"model_id": "model", "input_tokens": 5, "context_window_tokens": 10},
    })
    loaded = SimpleNamespace(
        history=history,
        path=tmp_path / "conversation.json",
        provider="deepseek",
        model="model",
        thinking="off",
        chat_items=[],
        telemetry=telemetry,
    )
    refreshed: list[bool] = []
    persistence.needs_status_refresh.connect(lambda: refreshed.append(True))

    persistence.apply_loaded(loaded)

    assert restored == [telemetry.to_dict()]
    assert refreshed == [True]


def test_new_conversation_resets_telemetry() -> None:
    resets: list[bool] = []
    bridge = _RestoreBridge()
    persistence = ConversationPersistence(
        bridge=bridge,
        chat=SimpleNamespace(reset=lambda: None),
        playground=SimpleNamespace(clear=lambda: None),
        input_panel=object(),
        left_pane=object(),
        settings=object(),
        get_conversation_telemetry=ConversationTelemetry,
        restore_conversation_telemetry=lambda _telemetry: None,
        reset_conversation_usage=lambda: resets.append(True),
    )

    persistence.new_conversation()

    assert resets == [True]
