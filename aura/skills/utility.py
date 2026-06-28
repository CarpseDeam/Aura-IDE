"""Derive-on-read utility meter for source context sources.

Opens the outcome-join DB (.aura/skill_outcomes.db) read-only every call,
computes per-source success lift within each terrain band (task_kind),
and returns a dict of SourceUtility values.

Silent-degrade contract: returns {} on any failure.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.conversation.dispatch import WorkerOutcomeStatus

_log = logging.getLogger(__name__)

_DB_FILENAME = "skill_outcomes.db"


@dataclass(frozen=True)
class SourceUtility:
    """Per-source utility measurement within one terrain band."""

    source_id: str
    task_kind: str
    loaded_n: int
    not_loaded_n: int
    lift: float | None
    status: str  # "measured" or "insufficient"


def _is_success_status(status: str) -> bool:
    """Return True if the status is a successful outcome."""
    return status in (
        WorkerOutcomeStatus.completed.value,
        WorkerOutcomeStatus.completed_with_caveats.value,
    )


def _compute_utility_from_rows(
    rows: list[dict[str, Any]],
    *,
    min_arm: int = 3,
) -> dict[str, SourceUtility]:
    """Pure computation: take raw DB rows, return per-source utility dict.

    Each row dict must have keys: 'included_source_ids' (JSON list of str),
    'status' (str), and 'task_kind' (str or None).
    """
    from collections import defaultdict

    # First pass: collect ALL unique source IDs across every row.
    all_source_ids: set[str] = set()
    parsed_rows: list[tuple[str, bool, frozenset[str]]] = []
    for row in rows:
        task_kind = row.get("task_kind") or "unknown"
        status = str(row.get("status") or "")
        is_success = _is_success_status(status)

        raw_ids = row.get("included_source_ids")
        if isinstance(raw_ids, str):
            try:
                source_ids: list[str] = json.loads(raw_ids)
            except (json.JSONDecodeError, TypeError):
                source_ids = []
        elif isinstance(raw_ids, list):
            source_ids = raw_ids
        else:
            source_ids = []

        included_set = frozenset(str(s) for s in source_ids)
        all_source_ids.update(included_set)
        parsed_rows.append((task_kind, is_success, included_set))

    if not all_source_ids:
        return {}

    # Second pass: for each known source, for each row in each terrain band,
    # count loaded vs not-loaded successes/totals.
    band_sources: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"loaded_ok": 0, "loaded_total": 0, "not_loaded_ok": 0, "not_loaded_total": 0})
    )

    for task_kind, is_success, included_set in parsed_rows:
        for sid in all_source_ids:
            band = band_sources[task_kind][sid]
            if sid in included_set:
                band["loaded_total"] += 1
                if is_success:
                    band["loaded_ok"] += 1
            else:
                band["not_loaded_total"] += 1
                if is_success:
                    band["not_loaded_ok"] += 1

    # For sources that appear in multiple bands, pick the band with the largest
    # combined sample size.
    source_bands: dict[str, tuple[str, dict[str, int]]] = {}
    for task_kind, sources in band_sources.items():
        for sid, counts in sources.items():
            combined = counts["loaded_total"] + counts["not_loaded_total"]
            if sid not in source_bands or combined > (
                source_bands[sid][1]["loaded_total"] + source_bands[sid][1]["not_loaded_total"]
            ):
                source_bands[sid] = (task_kind, counts)

    result: dict[str, SourceUtility] = {}
    for sid in sorted(source_bands):
        task_kind, counts = source_bands[sid]
        loaded_n = counts["loaded_total"]
        not_loaded_n = counts["not_loaded_total"]

        if loaded_n < min_arm or not_loaded_n < min_arm:
            result[sid] = SourceUtility(
                source_id=sid,
                task_kind=task_kind,
                loaded_n=loaded_n,
                not_loaded_n=not_loaded_n,
                lift=None,
                status="insufficient",
            )
        else:
            loaded_rate = counts["loaded_ok"] / loaded_n
            not_loaded_rate = counts["not_loaded_ok"] / not_loaded_n
            lift = loaded_rate - not_loaded_rate
            result[sid] = SourceUtility(
                source_id=sid,
                task_kind=task_kind,
                loaded_n=loaded_n,
                not_loaded_n=not_loaded_n,
                lift=lift,
                status="measured",
            )

    return result


def derive_source_utility(
    workspace_root: Path,
    *,
    min_arm: int = 3,
) -> dict[str, SourceUtility]:
    """Open the outcome-join DB read-only and compute per-source utility.

    Returns {} if the DB is absent or on any failure (silent-degrade contract).
    """
    db_path = workspace_root / ".aura" / _DB_FILENAME
    if not db_path.is_file():
        return {}

    try:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT status, task_kind, included_source_ids FROM outcome_joins"
            )
            rows = [dict(row) for row in cursor.fetchall()]
        finally:
            if conn is not None:
                conn.close()

        if not rows:
            return {}

        return _compute_utility_from_rows(rows, min_arm=min_arm)
    except Exception:
        _log.exception("derive_source_utility failed (degrading to empty)")
        return {}
