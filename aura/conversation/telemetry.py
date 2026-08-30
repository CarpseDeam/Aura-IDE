"""Durable, conversation-scoped provider usage telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _token_count(value: Any) -> int:
    """Normalize provider and persisted token counts without estimating them."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def context_window_for_model(model_id: str) -> int:
    """Return the catalogued capacity for *model_id*, or zero when unknown."""
    from aura.models import PROVIDERS

    for provider in PROVIDERS.values():
        model = provider.models.get(model_id)
        if model is not None:
            return _token_count(model.context_window_tokens)
    return 0


def provider_id_for_model(model_id: str) -> str:
    """Return the id of the provider that catalogs *model_id*, or "" when unknown."""
    from aura.models import PROVIDERS

    for provider in PROVIDERS.values():
        if model_id in provider.models:
            return provider.id
    return ""


def _decimal_rate(value: float) -> Decimal:
    """Convert a catalog/pricing-store float rate to Decimal without binary noise."""
    return Decimal(str(value))


def _price_event(
    *,
    provider_id: str,
    model_id: str,
    hit: int,
    miss: int,
    out: int,
    now: datetime,
) -> tuple[str, str, str, bool, bool, Decimal | None]:
    """Price one usage observation. Returns (tier, source_url, retrieved_at, stale, exact, cost)."""
    from aura.models import get_pricing
    from aura.providers.pricing import has_pricing_source, priced_lookup_for

    if provider_id and has_pricing_source(provider_id):
        lookup = priced_lookup_for(provider_id, model_id, now)
        if lookup is None:
            return "unknown", "", "", False, False, None
        rates = lookup.rates
        cost = (
            Decimal(hit) * _decimal_rate(rates["in_hit"])
            + Decimal(miss) * _decimal_rate(rates["in_miss"])
            + Decimal(out) * _decimal_rate(rates["out"])
        ) / Decimal(1_000_000)
        return lookup.tier, lookup.source_url, lookup.retrieved_at, lookup.stale, False, cost

    rates = get_pricing(model_id)
    if rates is None:
        return "unknown", "", "", False, False, None
    cost = (
        Decimal(hit) * _decimal_rate(rates["in_hit"])
        + Decimal(miss) * _decimal_rate(rates["in_miss"])
        + Decimal(out) * _decimal_rate(rates["out"])
    ) / Decimal(1_000_000)
    return "catalog", "", "", False, False, cost


@dataclass(frozen=True)
class UsageEvent:
    """One priced usage observation — the durable unit conversation cost is built from."""

    provider_id: str
    model_id: str
    timestamp: str  # ISO-8601 UTC
    cache_hit_tokens: int
    cache_miss_tokens: int
    output_tokens: int
    pricing_tier: str  # "off_peak" | "peak" | "catalog" | "unknown"
    source_url: str = ""
    retrieved_at: str = ""
    stale: bool = False
    exact: bool = False
    cost: str | None = None  # Decimal string in USD, or None when unpriceable

    def cost_decimal(self) -> Decimal | None:
        if self.cost is None:
            return None
        try:
            return Decimal(self.cost)
        except InvalidOperation:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "timestamp": self.timestamp,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "output_tokens": self.output_tokens,
            "pricing_tier": self.pricing_tier,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "stale": self.stale,
            "exact": self.exact,
            "cost": self.cost,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "UsageEvent | None":
        if not isinstance(data, dict):
            return None
        cost = data.get("cost")
        if cost is not None:
            try:
                Decimal(str(cost))
                cost = str(cost)
            except InvalidOperation:
                cost = None
        return cls(
            provider_id=str(data.get("provider_id", "")),
            model_id=str(data.get("model_id", "")),
            timestamp=str(data.get("timestamp", "")),
            cache_hit_tokens=_token_count(data.get("cache_hit_tokens")),
            cache_miss_tokens=_token_count(data.get("cache_miss_tokens")),
            output_tokens=_token_count(data.get("output_tokens")),
            pricing_tier=str(data.get("pricing_tier", "unknown")),
            source_url=str(data.get("source_url", "")),
            retrieved_at=str(data.get("retrieved_at", "")),
            stale=bool(data.get("stale", False)),
            exact=bool(data.get("exact", False)),
            cost=cost,
        )


@dataclass(frozen=True)
class CostSummary:
    """The footer's audit-ready view over a conversation's priced events."""

    known_total: Decimal | None
    unknown_count: int
    total_events: int
    exact: bool
    any_stale: bool


@dataclass
class LatestContext:
    """The most recently provider-confirmed input-token snapshot."""

    model_id: str = ""
    input_tokens: int = 0
    context_window_tokens: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "context_window_tokens": self.context_window_tokens,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "LatestContext":
        if not isinstance(data, dict):
            return cls()
        model_id = data.get("model_id")
        return cls(
            model_id=model_id if isinstance(model_id, str) else "",
            input_tokens=_token_count(data.get("input_tokens")),
            context_window_tokens=_token_count(data.get("context_window_tokens")),
        )


@dataclass
class ConversationTelemetry:
    """The sole normalized data shape for durable footer telemetry."""

    per_model: dict[str, dict[str, int]] = field(default_factory=dict)
    latest_context: LatestContext = field(default_factory=LatestContext)
    events: list[UsageEvent] = field(default_factory=list)

    def record_usage(
        self,
        *,
        model_id: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
        context_window_tokens: int,
        provider_id: str | None = None,
        update_latest_context: bool = True,
        now: datetime | None = None,
    ) -> None:
        """Accumulate and price usage, optionally replacing latest context.

        Pricing is calculated once, at the moment the usage is observed, from
        whichever pricing snapshot/tier is in force right now. It is never
        recalculated later, so persisted events stay accurate to the rates
        that actually applied when the call happened.
        """
        prompt = _token_count(prompt)
        hit = _token_count(hit)
        miss = _token_count(miss)
        completion = _token_count(completion)
        if hit == 0 and miss == 0:
            miss = prompt
        bucket = self.per_model.setdefault(model_id, {"hit": 0, "miss": 0, "out": 0})
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += completion
        if update_latest_context:
            self.latest_context = LatestContext(
                model_id=model_id,
                input_tokens=prompt,
                context_window_tokens=_token_count(context_window_tokens),
            )

        moment = now or datetime.now(timezone.utc)
        event_provider_id = str(provider_id or provider_id_for_model(model_id))
        tier, source_url, retrieved_at, stale, exact, cost = _price_event(
            provider_id=event_provider_id,
            model_id=model_id,
            hit=hit,
            miss=miss,
            out=completion,
            now=moment,
        )
        self.events.append(
            UsageEvent(
                provider_id=event_provider_id,
                model_id=model_id,
                timestamp=moment.isoformat(),
                cache_hit_tokens=hit,
                cache_miss_tokens=miss,
                output_tokens=completion,
                pricing_tier=tier,
                source_url=source_url,
                retrieved_at=retrieved_at,
                stale=stale,
                exact=exact,
                cost=str(cost) if cost is not None else None,
            )
        )

    def cost_summary(self) -> CostSummary:
        """Summarize priced events for footer display — never re-prices anything."""
        known_costs = [e.cost_decimal() for e in self.events]
        known = [c for c in known_costs if c is not None]
        unknown_count = len(self.events) - len(known)
        if not known:
            return CostSummary(
                known_total=None,
                unknown_count=unknown_count,
                total_events=len(self.events),
                exact=False,
                any_stale=False,
            )
        total = sum(known, Decimal(0))
        priced_events = [e for e in self.events if e.cost_decimal() is not None]
        exact = all(e.exact for e in priced_events)
        any_stale = any(e.stale for e in priced_events)
        return CostSummary(
            known_total=total,
            unknown_count=unknown_count,
            total_events=len(self.events),
            exact=exact,
            any_stale=any_stale,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_model": {model: dict(counts) for model, counts in self.per_model.items()},
            "latest_context": self.latest_context.to_dict(),
            "events": [e.to_dict() for e in self.events],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ConversationTelemetry":
        if not isinstance(data, dict):
            return cls()
        per_model_raw = data.get("per_model")
        per_model: dict[str, dict[str, int]] = {}
        if isinstance(per_model_raw, dict):
            for model_id, counts in per_model_raw.items():
                if isinstance(model_id, str) and isinstance(counts, dict):
                    per_model[model_id] = {
                        "hit": _token_count(counts.get("hit")),
                        "miss": _token_count(counts.get("miss")),
                        "out": _token_count(counts.get("out")),
                    }
        events_raw = data.get("events")
        events: list[UsageEvent] = []
        if isinstance(events_raw, list):
            for item in events_raw:
                event = UsageEvent.from_dict(item)
                if event is not None:
                    events.append(event)
        return cls(
            per_model=per_model,
            latest_context=LatestContext.from_dict(data.get("latest_context")),
            events=events,
        )
