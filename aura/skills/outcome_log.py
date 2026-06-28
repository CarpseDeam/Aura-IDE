"""Append-only outcome-join writer.

Phase 1 of the outcome-join capture: writes one row per completed worker
dispatch joining the loaded-context ledger with the dispatch outcome.
Nothing reads this table yet — Phase 2 builds the reader.

Contract: silently degrades to no-op on any failure, never propagates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_DB_FILENAME = "skill_outcomes.db"


def record_outcome_join(
    workspace_root: Path,
    *,
    tool_call_id: str,
    status: str,
    worker_model: str,
    task_kind: str | None,
    target_files: list[str],
    ledger: dict[str, Any] | None,
) -> None:
    """Append one outcome-join row to the skill outcomes database.

    Derives the included-source-ids by reading the ledger dict's entries
    — the same serialized shape *context_gearbox_metadata* produces, where
    each entry has *source_id* and *included*.

    Degrades to a silent no-op on any failure (identical contract to
    *record_hazard*).  A malformed or empty ledger writes a row with an
    empty source-ids list rather than raising.
    """
    try:
        # Derive included source ids from the ledger entries where
        # included is True.
        included_source_ids: list[str] = []
        if isinstance(ledger, dict):
            entries = ledger.get("ledger")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("included"):
                        sid = entry.get("source_id")
                        if isinstance(sid, str) and sid:
                            included_source_ids.append(sid)

        db_dir = workspace_root / ".aura"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / _DB_FILENAME

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS outcome_joins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_call_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_model TEXT NOT NULL,
                    task_kind TEXT,
                    target_files TEXT NOT NULL,
                    included_source_ids TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_oj_tool_call_id
                   ON outcome_joins (tool_call_id)"""
            )

            conn.execute(
                """INSERT INTO outcome_joins
                   (tool_call_id, status, worker_model, task_kind,
                    target_files, included_source_ids,
                    schema_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tool_call_id,
                    status,
                    worker_model,
                    task_kind,
                    json.dumps(sorted(target_files)),
                    json.dumps(included_source_ids),
                    SCHEMA_VERSION,
                    datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
    except Exception:
        _log.exception("record_outcome_join failed (degrading to no-op)")
