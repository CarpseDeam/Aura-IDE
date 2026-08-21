"""DeepSeek pricing source — official rates from the published pricing page.

Retrieves ``https://api-docs.deepseek.com/quick_start/pricing/`` and parses
it semantically: the pricing table's MODEL header row names the model
columns, and rows labeled with rate kinds (CACHE HIT / CACHE MISS / OUTPUT
TOKENS) plus OFF-PEAK / PEAK tiers carry the per-model prices. Each row's
cells are aligned to the model header row of the same table — no fixed
column positions and no document-wide regex scraping — so a reordered or
extended model table still parses, while a structurally broken page is
rejected and the previous validated cache survives.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

from aura.providers.pricing import (
    ModelRates,
    PeakWindow,
    PricingResult,
    RateTier,
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


def _norm(text: str) -> str:
    return " ".join(text.split()).strip()


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
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
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
            return PricingResult(
                provider_id=self.provider_id,
                models=built,
                source_url=self.source_url,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                peak_windows=peak_windows,
            )
        return None
