"""DeepSeek pricing source — official rates from the published pricing page.

Retrieves ``https://api-docs.deepseek.com/quick_start/pricing/`` and parses
it semantically: the pricing table's MODEL header row names the model
columns, and rows labeled with rate kinds (CACHE HIT / CACHE MISS / OUTPUT
TOKENS) plus OFF-PEAK / PEAK tiers carry the per-model prices. Each row's
cells are aligned to the model header row of the same table — no fixed
column positions and no document-wide regex scraping — so a reordered or
extended model table still parses, while a structurally broken page is
rejected and the previous validated cache survives.

The page also publishes an effective-dated Beijing-Time weekly rule (e.g.
"Saturday and Sunday in Beijing Time are entirely off-peak"). That wording
is DeepSeek-specific and interpreted only here; it's normalized into the
shared, provider-neutral :class:`~aura.providers.pricing.ScheduleOverride`
shape before being handed to :class:`~aura.providers.pricing.PricingResult`
— the shared pricing module only ever evaluates that normalized schedule,
never DeepSeek's wording directly. Beijing Time is treated as a fixed
UTC+08:00 calendar offset (it observes no DST), so parsing never depends on
the host's timezone database or locale.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx

from aura.providers.pricing import (
    ModelRates,
    PeakWindow,
    PricingResult,
    RateTier,
    ScheduleOverride,
)

DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"

_FETCH_TIMEOUT = 15.0

_MODEL_HEADER = "MODEL"
_RATE_KINDS: tuple[tuple[str, str], ...] = (
    ("CACHE HIT", "in_hit"),
    ("CACHE MISS", "in_miss"),
    ("OUTPUT TOKENS", "out"),
)
_PRICE_RE = re.compile(r"^\$?\s*(\d+(?:\.\d+)?)$")
_TIER_RE = re.compile(r"^(OFF[- ]?PEAK|PEAK)$")
_TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")
_PEAK_HOURS_RE = re.compile(r"peak\s*hours", re.IGNORECASE)
_SCHEDULE_CONTEXT_CHARS = 500

_BEIJING_OFFSET_MINUTES = 8 * 60  # UTC+08:00, fixed — Beijing observes no DST

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_BEIJING_MENTION_RE = re.compile(r"beijing time", re.IGNORECASE)
# "Effective 00:00 (Beijing Time) on Sunday, August 23, 2026, ..." — the
# parenthetical around "Beijing Time" is optional so either phrasing matches.
_EFFECTIVE_BEIJING_RE = re.compile(
    r"Effective\s+(\d{1,2}):(\d{2})\s*\(?Beijing Time\)?\s+on\s+[A-Za-z]+,\s+"
    r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})",
    re.IGNORECASE,
)
# "... off-peak rates applying throughout the day on weekends (Saturdays and
# Sundays, Beijing Time)." — the tier and the day-name list both come from
# this clause; the parenthetical must itself confirm Beijing Time so an
# unrelated "weekends" mention elsewhere on the page can't match.
_ALL_DAY_TIER_RULE_RE = re.compile(
    r"(off[- ]?peak|peak)\s+rates?\s+applying\s+throughout\s+the\s+day\s+on\s+weekends?\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return " ".join(text.split()).strip()


def _page_text(html: str) -> str:
    """Strip tags/scripts/styles down to whitespace-normalized visible text."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


class _TableParser(HTMLParser):
    """Collect the cell text of every table in the document, row by row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _parse_pricing_table(rows: list[list[str]]) -> tuple[list[str], dict[str, dict[str, dict[str, float]]]] | None:
    """Semantically interpret one table: MODEL header row + priced tier rows.

    Returns ``(model_ids, {kind: {model_id: {tier: value}}})`` or None when
    the table is not (or no longer looks like) the pricing table.
    """
    model_row: list[str] | None = None
    for row in rows:
        cells = [_norm(c) for c in row]
        if cells and cells[0] == _MODEL_HEADER:
            model_row = cells
            break
    if model_row is None:
        return None
    models = [c for c in model_row[1:] if c]
    if not models:
        return None

    values: dict[str, dict[str, dict[str, float]]] = {"in_hit": {}, "in_miss": {}, "out": {}}
    current_kind: str | None = None

    for row in rows:
        cells = [_norm(c) for c in row]
        if not cells or cells[0] == _MODEL_HEADER:
            continue

        # A rate-kind label (the row label spanning its OFF-PEAK/PEAK
        # sub-rows) sets the kind for the rows that follow it.
        for label, kind in _RATE_KINDS:
            if any(label in c for c in cells):
                current_kind = kind
                break

        # Trailing run of price cells, aligned to the model header columns.
        prices: list[float] = []
        idx = len(cells)
        while idx > 0:
            match = _PRICE_RE.match(cells[idx - 1])
            if match is None:
                break
            prices.append(float(match.group(1)))
            idx -= 1
        prices.reverse()
        if not prices or len(prices) != len(models) or current_kind is None or idx == 0:
            continue

        tier_match = _TIER_RE.match(cells[idx - 1])
        if tier_match is None:
            # A numeric row without an OFF-PEAK/PEAK label (e.g. the
            # concurrency limits row) is not a pricing row.
            continue
        tier = "peak" if tier_match.group(1).startswith("PEAK") else "off_peak"

        for model, value in zip(models, prices):
            values[current_kind].setdefault(model, {})[tier] = value

    return models, values


def _build_model_rates(
    models: list[str],
    values: dict[str, dict[str, dict[str, float]]],
) -> dict[str, ModelRates] | None:
    """Assemble per-model rates; None when any model's data is incomplete."""
    built: dict[str, ModelRates] = {}
    for model in models:
        try:
            off_peak = RateTier(
                in_miss=values["in_miss"][model]["off_peak"],
                in_hit=values["in_hit"][model]["off_peak"],
                out=values["out"][model]["off_peak"],
            )
        except KeyError:
            return None
        peak_values = [values[kind][model].get("peak") for kind in ("in_miss", "in_hit", "out")]
        if all(v is not None for v in peak_values):
            peak = RateTier(in_miss=peak_values[0], in_hit=peak_values[1], out=peak_values[2])
        elif any(v is not None for v in peak_values):
            return None  # partially peak-priced model — incomplete page
        else:
            peak = None
        built[model] = ModelRates(model_id=model, off_peak=off_peak, peak=peak)
    return built


def _parse_peak_windows(html: str) -> tuple[PeakWindow, ...] | None:
    """Parse the published peak/off-peak UTC schedule from the page text.

    Returns the windows, ``()`` when the page publishes no schedule, or None
    when a published schedule cannot be parsed (the caller rejects the
    whole result so the previous valid cache survives).
    """
    text = _page_text(html)
    match = _PEAK_HOURS_RE.search(text)
    if match is None:
        return ()
    window_text = text[match.start() : match.start() + _SCHEDULE_CONTEXT_CHARS]
    windows: list[PeakWindow] = []
    for m in _TIME_RANGE_RE.finditer(window_text):
        start = int(m.group(1)) * 60 + int(m.group(2))
        end = int(m.group(3)) * 60 + int(m.group(4))
        if end == 0 and start > 0:
            # "16:30 - 00:00 UTC" means peak until midnight.
            end = 24 * 60
        windows.append(PeakWindow(start_minute=start, end_minute=end))
    if not windows:
        return None
    return tuple(windows)


def _weekday_index(name: str) -> int | None:
    """Map a (possibly plural, e.g. "Saturdays") day name to Mon=0..Sun=6."""
    key = name.strip().lower()
    if key.endswith("s") and key[:-1] in _WEEKDAY_NAMES:
        key = key[:-1]
    return _WEEKDAY_NAMES.get(key)


def _parse_schedule_overrides(html: str) -> tuple[ScheduleOverride, ...] | None:
    """Parse DeepSeek's effective-dated Beijing-Time weekly rule, if any.

    Looks for wording of the shape "Effective HH:MM (Beijing Time) on
    <Weekday>, <Month> <Day>, <Year> ... <tier> rates applying throughout the
    day on weekends (<Weekday list>, Beijing Time)." Returns the resulting
    override(s), ``()`` when the page makes no Beijing-Time claim at all, or
    None when it does but either the effective date or the weekday/tier rule
    can't be parsed in full — the caller then rejects the whole fetch rather
    than silently dropping the rule.
    """
    text = _page_text(html)
    if _BEIJING_MENTION_RE.search(text) is None:
        return ()

    effective_match = _EFFECTIVE_BEIJING_RE.search(text)
    weekly_match = _ALL_DAY_TIER_RULE_RE.search(text)
    if effective_match is None or weekly_match is None:
        return None

    hour, minute, month_name, day, year = effective_match.groups()
    month = _MONTH_NAMES.get(month_name.strip().lower())
    if month is None:
        return None
    try:
        beijing_naive = datetime(int(year), month, int(day), int(hour), int(minute))
    except ValueError:
        return None
    effective_utc = (beijing_naive - timedelta(minutes=_BEIJING_OFFSET_MINUTES)).replace(tzinfo=timezone.utc)

    tier_text, days_clause = weekly_match.groups()
    if _BEIJING_MENTION_RE.search(days_clause) is None:
        return None  # the day-name parenthetical must itself confirm Beijing Time
    weekdays = [
        idx
        for token in re.findall(r"[A-Za-z]+", days_clause)
        if (idx := _weekday_index(token)) is not None
    ]
    if not weekdays:
        return None
    normalized_tier = tier_text.strip().lower().replace(" ", "").replace("-", "")
    tier = "peak" if normalized_tier == "peak" else "off_peak"

    return (
        ScheduleOverride(
            effective_at=effective_utc.isoformat(),
            utc_offset_minutes=_BEIJING_OFFSET_MINUTES,
            weekdays=tuple(sorted(set(weekdays))),
            tier=tier,
        ),
    )


class DeepSeekPricingSource:
    """Retrieve DeepSeek's official model rates from its pricing page."""

    provider_id = "deepseek"
    source_url = DEEPSEEK_PRICING_URL

    def fetch(self) -> PricingResult | None:
        """Live retrieval — read-only GET; None on any failure."""
        try:
            response = httpx.get(self.source_url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
            response.raise_for_status()
        except Exception:
            return None
        return self.parse(response.text)

    def parse(self, html: str) -> PricingResult | None:
        """Parse a pricing page snapshot into a normalized result, or None."""
        parser = _TableParser()
        try:
            parser.feed(html)
        except Exception:
            return None
        for rows in parser.tables:
            parsed = _parse_pricing_table(rows)
            if parsed is None:
                continue
            models, values = parsed
            built = _build_model_rates(models, values)
            if built is None:
                return None  # incomplete page — preserve the previous valid cache
            peak_windows = _parse_peak_windows(html)
            if peak_windows is None:
                return None
            schedule_overrides = _parse_schedule_overrides(html)
            if schedule_overrides is None:
                return None
            return PricingResult(
                provider_id=self.provider_id,
                models=built,
                source_url=self.source_url,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                peak_windows=peak_windows,
                schedule_overrides=schedule_overrides,
            )
        return None
