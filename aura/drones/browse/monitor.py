"""Monitor fingerprint substrate for Browse Drone page state tracking.

Provides stable fingerprinting of page snapshots and persistent storage
by monitor_key so that subsequent runs can detect whether a page has
changed, is unchanged, or is being seen for the first time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re as _re
import os
import tempfile
from pathlib import Path
from typing import Any

from aura.drones.browse.models import BrowseSnapshot

logger = logging.getLogger(__name__)

_SUPPORTED_FIELDS = frozenset({"title", "url", "body_excerpt"})


def normalize_text(text: str) -> str:
    """Collapse all whitespace sequences to single spaces and strip."""
    return _re.sub(r"\s+", " ", text).strip()


def distill_snapshot(
    snapshot: BrowseSnapshot | dict,
    fields: list[str] | None = None,
    excerpt_chars: int = 2000,
) -> dict[str, Any]:
    """Extract a stable, minimal dict of selected fields from a snapshot.

    Supported fields are ``"title"``, ``"url"``, and ``"body_excerpt"``.
    Unknown fields are silently ignored.
    The ``body_excerpt`` value is normalised and capped to ``excerpt_chars``.
    """
    if fields is None:
        fields = ["title", "url", "body_excerpt"]

    if isinstance(snapshot, BrowseSnapshot):
        raw = snapshot.to_dict()
    else:
        raw = snapshot

    result: dict[str, Any] = {}
    for field in fields:
        if field not in _SUPPORTED_FIELDS:
            continue
        value = raw.get(field, "")
        if field == "body_excerpt" and value:
            value = normalize_text(value)[:excerpt_chars]
        elif isinstance(value, str):
            value = value.strip()
        result[field] = value
    return result


def fingerprint_state(state: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 hex digest of a state dict.

    Uses JSON serialisation with ``sort_keys=True`` so that equivalent
    dicts always produce the same fingerprint.
    """
    canonical = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def monitor_store_path(workspace_root: Path) -> Path:
    """Return the path to the browse monitor state JSON file."""
    return workspace_root / ".aura" / "browse_monitor_state.json"


def load_monitor_state(workspace_root: Path) -> dict[str, Any]:
    """Load the persisted monitor state, failing soft to an empty dict."""
    path = monitor_store_path(workspace_root)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Failed to load browse monitor state from %s", path)
        return {}


def save_monitor_state(workspace_root: Path, state: dict[str, Any]) -> None:
    """Persist the monitor state dict to disk atomically.

    Creates parent directories if needed.
    Writes JSON with ``indent=2`` and sorted keys.
    Uses a tempfile + replace pattern for atomic writes.
    """
    path = monitor_store_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
    Path(tmp_path).replace(path)


def update_monitor_fingerprint(
    workspace_root: Path,
    monitor_key: str,
    snapshot: BrowseSnapshot | dict,
    fields: list[str] | None = None,
    excerpt_chars: int = 2000,
) -> dict[str, Any]:
    """Compute a fingerprint for *snapshot*, compare with any previous
    fingerprint stored for *monitor_key*, persist the new state, and
    return a monitor-result dict.

    Returns::

        {
            "enabled": true,
            "monitor_key": "<key>",
            "verdict": "first_seen" | "unchanged" | "changed",
            "fingerprint": "<sha256>",
            "previous_fingerprint": "<sha256>" | None,
            "state": {<distilled fields>},
            "changed_at": "<utc-iso-timestamp>",
        }
    """
    if fields is None:
        fields = ["title", "url", "body_excerpt"]

    state = distill_snapshot(snapshot, fields, excerpt_chars)
    new_fp = fingerprint_state(state)

    # Load existing monitor state
    monitor_data = load_monitor_state(workspace_root)
    previous_entry = monitor_data.get(monitor_key)
    previous_fp = previous_entry.get("fingerprint") if previous_entry else None

    if previous_fp is None:
        verdict = "first_seen"
    elif new_fp == previous_fp:
        verdict = "unchanged"
    else:
        verdict = "changed"

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    # Persist the new state for this key
    monitor_data[monitor_key] = {
        "fingerprint": new_fp,
        "state": state,
        "updated_at": now_iso,
    }
    save_monitor_state(workspace_root, monitor_data)

    return {
        "enabled": True,
        "monitor_key": monitor_key,
        "verdict": verdict,
        "fingerprint": new_fp,
        "previous_fingerprint": previous_fp,
        "state": state,
        "changed_at": now_iso,
    }


def apply_monitor_to_artifact(
    artifact: dict[str, Any],
    *,
    workspace_root: Path,
    monitor_key: str,
    monitor_fields: list[str] | None = None,
    monitor_excerpt_chars: int = 2000,
    snapshot: BrowseSnapshot | dict | None = None,
) -> str:
    """Apply monitor fingerprint to an artifact dict.

    When *snapshot* is not ``None``, calls :func:`update_monitor_fingerprint`
    and stores the result in ``artifact["monitor"]``.

    When *snapshot* is ``None``, sets ``artifact["monitor"]`` to a
    ``not_checked`` placeholder.

    Returns the verdict string (empty string for ``not_checked``).
    """
    if snapshot is not None:
        result = update_monitor_fingerprint(
            workspace_root=workspace_root,
            monitor_key=monitor_key,
            snapshot=snapshot,
            fields=monitor_fields,
            excerpt_chars=monitor_excerpt_chars,
        )
        artifact["monitor"] = result
        return result["verdict"]

    artifact["monitor"] = {
        "enabled": True,
        "monitor_key": monitor_key,
        "verdict": "not_checked",
        "reason": "No snapshot available",
    }
    return ""
