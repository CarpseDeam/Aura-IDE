"""Provider pricing-source boundary — normalized official rates for the cost path.

A provider may register a pricing source that retrieves its official
published rates. A source returns a validated :class:`PricingResult` —
per-model normalized rates plus the source URL and retrieval time, and any
peak/off-peak UTC schedule the page publishes. The store keeps the last
validated fetched or cached result per provider; the cost path
(``aura.models.get_pricing`` → ``cost_usd`` → status bar) reads only this
store for sourced providers — never the network, and never at repaint time.

Only providers with a registered source participate; OpenRouter and every
other provider keep their catalog pricing untouched. A future provider
supplies the same normalized result by registering its own source class —
no status-bar or cost-calculation change needed.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Protocol

from aura.paths import config_dir

logger = logging.getLogger(__name__)

#: A last-known-good pricing result older than this is surfaced to the user
#: as stale rather than silently presented as current. Provider pricing pages
#: change rarely, so this is a generous window, not a refresh interval.
STALE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class RateTier:
    """One set of per-million-token rates, in USD."""

    in_miss: float
    in_hit: float
    out: float


@dataclass(frozen=True)
class ModelRates:
    """Normalized rates for one model id."""

    model_id: str
    off_peak: RateTier
    #: Peak-hour tier; None when the page publishes no peak pricing.
    peak: RateTier | None = None


@dataclass(frozen=True)
class PeakWindow:
    """One daily peak window, in UTC minutes since 00:00 (end exclusive)."""

    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class ScheduleOverride:
    """An effective-dated, all-day weekly tier override.

    From ``effective_at`` onward, on any weekday named in ``weekdays`` —
    evaluated in the fixed ``utc_offset_minutes`` calendar, not UTC — the
    override's ``tier`` applies for that entire local day, superseding the
    recurring daily ``peak_windows``. Before the effective instant, or on a
    weekday this override doesn't name, the daily UTC peak windows apply as
    usual. A source (e.g. DeepSeek) is responsible for deciding what its
    page's wording means; this shape only records the resulting rule for the
    shared pricing module to evaluate.
    """

    effective_at: str  # ISO-8601 UTC timestamp
    utc_offset_minutes: int  # fixed calendar offset, e.g. 480 for UTC+08:00
    weekdays: tuple[int, ...]  # Mon=0 .. Sun=6, in the offset's local calendar
    tier: str  # "off_peak" | "peak"


@dataclass(frozen=True)
class PricingResult:
    """A fully validated pricing snapshot from one provider source."""

    provider_id: str
    models: dict[str, ModelRates]
    source_url: str
    retrieved_at: str  # ISO-8601 UTC timestamp
    peak_windows: tuple[PeakWindow, ...] = ()
    schedule_overrides: tuple[ScheduleOverride, ...] = ()


class PricingSource(Protocol):
    """A provider-owned source of official published rates.

    ``fetch`` retrieves and parses the provider's pricing page and returns a
    fully built ``PricingResult``, or None when retrieval/parsing failed so
    the previous valid cache is preserved.
    """

    provider_id: ClassVar[str]
    source_url: ClassVar[str]

    def fetch(self) -> PricingResult | None:
        ...


# Registered source classes, keyed by provider id. Registration happens in
# aura/providers/pricing_sources (imported by aura.config at startup), and is
# self-healing via get_source() so callers never depend on import order.
_SOURCES: dict[str, type[PricingSource]] = {}

# Validated results currently in force, keyed by provider id.
_results: dict[str, PricingResult] = {}


def register_source(source_cls: type[PricingSource]) -> None:
    """Register a pricing source class for its provider id."""
    _SOURCES[source_cls.provider_id] = source_cls


def _load_sources() -> None:
    """Import the sources package so every available source registers."""
    from aura.providers import pricing_sources  # noqa: F401


def get_source(provider_id: str) -> type[PricingSource] | None:
    if not _SOURCES:
        _load_sources()
    return _SOURCES.get(provider_id)


def has_pricing_source(provider_id: str) -> bool:
    """True when a provider supplies rates through the pricing boundary."""
    return get_source(provider_id) is not None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _valid_tier(tier: RateTier) -> bool:
    return all(
        isinstance(v, float) and math.isfinite(v) and v > 0.0
        for v in (tier.in_miss, tier.in_hit, tier.out)
    )


def _valid_schedule_override(override: ScheduleOverride, peak_any: bool) -> bool:
    try:
        effective = datetime.fromisoformat(override.effective_at)
    except (TypeError, ValueError):
        return False
    if effective.tzinfo is None:
        return False
    if not (-12 * 60 <= override.utc_offset_minutes <= 14 * 60):
        return False
    if override.tier not in ("off_peak", "peak"):
        return False
    if override.tier == "peak" and not peak_any:
        return False
    if not override.weekdays:
        return False
    if len(set(override.weekdays)) != len(override.weekdays):
        return False
    if any(not (0 <= d <= 6) for d in override.weekdays):
        return False
    return True


def _validate(result: PricingResult) -> bool:
    """Validate the entire result before it may replace the current cache.

    Strict by design: a page that dropped a model column, lost a tier, or
    changed shape must not silently supply partial rates — the previous
    valid result wins.
    """
    if not result.provider_id or not result.source_url or not result.retrieved_at:
        return False
    if not result.models:
        return False
    peak_any = any(mr.peak is not None for mr in result.models.values())
    peak_all = all(mr.peak is not None for mr in result.models.values())
    if peak_any != peak_all:
        # Mixed tiering (some models peak-priced, others not) is incomplete.
        return False
    if peak_any and not result.peak_windows:
        return False
    if not peak_any and result.peak_windows:
        return False
    for mr in result.models.values():
        if not mr.model_id:
            return False
        if not _valid_tier(mr.off_peak):
            return False
        if mr.peak is not None and not _valid_tier(mr.peak):
            return False
    for window in result.peak_windows:
        if not (0 <= window.start_minute < window.end_minute <= 24 * 60):
            return False
    for override in result.schedule_overrides:
        if not _valid_schedule_override(override, peak_any):
            return False
    return True


# ---------------------------------------------------------------------------
# Store — the only rates the cost path reads for sourced providers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _in_peak_window(now: datetime, windows: tuple[PeakWindow, ...]) -> bool:
    minute = now.hour * 60 + now.minute
    return any(w.start_minute <= minute < w.end_minute for w in windows)


def _local_weekday(now: datetime, utc_offset_minutes: int) -> int:
    """The calendar weekday (Mon=0..Sun=6) of *now* shifted by a fixed offset.

    *now* is treated as UTC wall-clock (the convention already used
    throughout this module); the shift is pure arithmetic, so this never
    depends on the host's timezone database.
    """
    return (now + timedelta(minutes=utc_offset_minutes)).weekday()


def _override_tier(rates: ModelRates, override: ScheduleOverride, now: datetime) -> RateTier | None:
    """The tier *override* selects for *rates* at *now*, or None when it doesn't apply."""
    try:
        effective = datetime.fromisoformat(override.effective_at)
    except (TypeError, ValueError):
        return None
    if now < effective:
        return None
    if _local_weekday(now, override.utc_offset_minutes) not in override.weekdays:
        return None
    return rates.peak if override.tier == "peak" else rates.off_peak


def _select_tier(rates: ModelRates, result: PricingResult, now: datetime) -> tuple[RateTier, str]:
    """Pick the active rate tier for *rates* at *now*, and name it.

    A matching schedule override (effective and naming today's local
    weekday) takes precedence over the recurring daily UTC peak windows.
    """
    for override in result.schedule_overrides:
        tier = _override_tier(rates, override, now)
        if tier is not None:
            return tier, override.tier
    if rates.peak is not None and result.peak_windows and _in_peak_window(now, result.peak_windows):
        return rates.peak, "peak"
    return rates.off_peak, "off_peak"


def rates_for(provider_id: str, model_id: str, now: datetime | None = None) -> dict[str, float] | None:
    """Return normalized ``{in_miss, in_hit, out}`` rates for a model, or None.

    Peak/off-peak tier is selected from the published UTC schedule and the
    current UTC time; with no schedule, the off-peak tier applies. None means
    "unavailable" — the caller's existing unavailable-price behavior applies.
    """
    result = _results.get(provider_id)
    if result is None:
        return None
    rates = result.models.get(model_id)
    if rates is None:
        return None
    tier, _name = _select_tier(rates, result, now or _utcnow())
    return {"in_miss": tier.in_miss, "in_hit": tier.in_hit, "out": tier.out}


def is_stale(result: PricingResult, now: datetime | None = None) -> bool:
    """True when *result* is older than :data:`STALE_AFTER`, or unparsable."""
    try:
        retrieved = datetime.fromisoformat(result.retrieved_at)
    except (TypeError, ValueError):
        return True
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=timezone.utc)
    return (now or _utcnow()) - retrieved > STALE_AFTER


@dataclass(frozen=True)
class PricedLookup:
    """A rate lookup carrying its tier and provenance, for per-event costing."""

    rates: dict[str, float]
    tier: str  # "peak" | "off_peak"
    source_url: str
    retrieved_at: str
    stale: bool


def priced_lookup_for(provider_id: str, model_id: str, now: datetime | None = None) -> PricedLookup | None:
    """Return the active rates plus tier/provenance/staleness for one event.

    Same tier-selection rule as :func:`rates_for`; returned alongside the
    source URL, retrieval time, and staleness so a usage event can persist
    exactly which snapshot priced it.
    """
    result = _results.get(provider_id)
    if result is None:
        return None
    rates = result.models.get(model_id)
    if rates is None:
        return None
    now = now or _utcnow()
    tier, tier_name = _select_tier(rates, result, now)
    return PricedLookup(
        rates={"in_miss": tier.in_miss, "in_hit": tier.in_hit, "out": tier.out},
        tier=tier_name,
        source_url=result.source_url,
        retrieved_at=result.retrieved_at,
        stale=is_stale(result, now),
    )


def get_result(provider_id: str) -> PricingResult | None:
    """Return the validated result currently in force for a provider."""
    return _results.get(provider_id)


# ---------------------------------------------------------------------------
# Last-known-good cache (platform-portable, config-dir based)
# ---------------------------------------------------------------------------


#: Bumped when the cache-entry shape changes. An entry whose stored version
#: doesn't match is rejected outright — no migration, since a mis-shaped
#: schedule override could silently mean the wrong rates.
_CACHE_SCHEMA_VERSION = 2


def pricing_cache_path() -> Path:
    return config_dir() / "pricing_cache.json"


def _tier_to_dict(tier: RateTier) -> dict[str, float]:
    return {"in_miss": tier.in_miss, "in_hit": tier.in_hit, "out": tier.out}


def _tier_from_dict(data: Any) -> RateTier | None:
    try:
        return RateTier(
            in_miss=float(data["in_miss"]),
            in_hit=float(data["in_hit"]),
            out=float(data["out"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _model_rates_to_dict(rates: ModelRates) -> dict[str, Any]:
    data: dict[str, Any] = {"model_id": rates.model_id, "off_peak": _tier_to_dict(rates.off_peak)}
    if rates.peak is not None:
        data["peak"] = _tier_to_dict(rates.peak)
    return data


def _model_rates_from_dict(data: Any) -> ModelRates | None:
    if not isinstance(data, dict):
        return None
    off_peak = _tier_from_dict(data.get("off_peak"))
    if off_peak is None:
        return None
    peak = _tier_from_dict(data["peak"]) if data.get("peak") is not None else None
    return ModelRates(model_id=str(data.get("model_id", "")), off_peak=off_peak, peak=peak)


def _schedule_override_to_dict(override: ScheduleOverride) -> dict[str, Any]:
    return {
        "effective_at": override.effective_at,
        "utc_offset_minutes": override.utc_offset_minutes,
        "weekdays": list(override.weekdays),
        "tier": override.tier,
    }


def _schedule_override_from_dict(data: Any) -> ScheduleOverride | None:
    if not isinstance(data, dict):
        return None
    try:
        return ScheduleOverride(
            effective_at=str(data["effective_at"]),
            utc_offset_minutes=int(data["utc_offset_minutes"]),
            weekdays=tuple(int(d) for d in data["weekdays"]),
            tier=str(data["tier"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _result_to_dict(result: PricingResult) -> dict[str, Any]:
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "provider_id": result.provider_id,
        "source_url": result.source_url,
        "retrieved_at": result.retrieved_at,
        "peak_windows": [[w.start_minute, w.end_minute] for w in result.peak_windows],
        "schedule_overrides": [_schedule_override_to_dict(o) for o in result.schedule_overrides],
        "models": {mid: _model_rates_to_dict(mr) for mid, mr in result.models.items()},
    }


def _result_from_dict(provider_id: str, data: Any) -> PricingResult | None:
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != _CACHE_SCHEMA_VERSION:
        # Reject rather than migrate: an older (or newer/unknown) cache shape
        # may not carry a schedule override correctly, so treat it as absent.
        return None
    try:
        models: dict[str, ModelRates] = {}
        for mid, m_data in data.get("models", {}).items():
            mr = _model_rates_from_dict(m_data)
            if mr is None or mr.model_id != mid:
                return None
            models[mid] = mr
        windows = tuple(
            PeakWindow(start_minute=int(w[0]), end_minute=int(w[1]))
            for w in data.get("peak_windows", [])
        )
        overrides_raw = data.get("schedule_overrides", [])
        if not isinstance(overrides_raw, list):
            return None
        overrides: list[ScheduleOverride] = []
        for o_data in overrides_raw:
            override = _schedule_override_from_dict(o_data)
            if override is None:
                return None
            overrides.append(override)
        return PricingResult(
            provider_id=provider_id,
            models=models,
            source_url=str(data.get("source_url", "")),
            retrieved_at=str(data.get("retrieved_at", "")),
            peak_windows=windows,
            schedule_overrides=tuple(overrides),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _save_cache(result: PricingResult) -> None:
    """Persist a validated result into the shared cache file.

    Only ever called with a fully validated result, so a partial or broken
    fetch can never overwrite a last-known-good cache entry.
    """
    path = pricing_cache_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
    data[result.provider_id] = _result_to_dict(result)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_cached_entry(provider_id: str) -> None:
    """Load one provider's last-known-good entry from disk into the store."""
    path = pricing_cache_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    entry = data.get(provider_id)
    result = _result_from_dict(provider_id, entry)
    if result is not None and _validate(result):
        _results[provider_id] = result


def load_pricing_cache() -> None:
    """Load every cached pricing entry at startup (mirrors load_dynamic_catalog)."""
    path = pricing_cache_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    for provider_id in data:
        _load_cached_entry(provider_id)


def refresh_provider_pricing(provider_id: str) -> PricingResult | None:
    """Fetch and validate a provider's official rates once.

    Called once per launch by the startup pricing controller, and from the
    Settings -> Models discovery flow — both off the UI thread, and never
    from the status bar or a cost calculation. A network failure, malformed
    page, missing model, or incomplete result preserves the previous valid
    result, falling back to the last-known-good cache when nothing valid is
    in memory. Returns the result now in force, or None when there is none.
    """
    source_cls = get_source(provider_id)
    if source_cls is None:
        return None
    fetched: PricingResult | None = None
    try:
        fetched = source_cls().fetch()
    except Exception as exc:
        logger.warning("Pricing fetch failed for %s: %s", provider_id, exc)
    if fetched is not None:
        if fetched.provider_id != provider_id or not _validate(fetched):
            logger.warning(
                "Pricing fetch for %s failed validation; keeping previous valid result.",
                provider_id,
            )
            fetched = None
    if fetched is None:
        if provider_id not in _results:
            _load_cached_entry(provider_id)
        return _results.get(provider_id)
    _results[provider_id] = fetched
    try:
        _save_cache(fetched)
    except OSError as exc:
        logger.warning("Could not persist pricing cache for %s: %s", provider_id, exc)
    return fetched
