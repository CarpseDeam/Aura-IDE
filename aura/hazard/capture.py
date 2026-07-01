from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aura.hazard.models import HazardRecord
from aura.hazard.store import HazardStore

_log = logging.getLogger(__name__)

HAZARD_STATUSES = frozenset({
    "validation_failed",
    "harness_error",
    "edit_mechanics_blocked",
})


def record_hazard(
    *,
    workspace_root: Path,
    model: str,
    status: str,
    structured_failure: dict[str, Any] | None = None,
    target_files: list[str] | None = None,
    task_shape: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    tool_call_id: str = "",
) -> int | None:
    try:
        if status not in HAZARD_STATUSES:
            return None

        failure_class: str | None = None
        if isinstance(structured_failure, dict):
            failure_class = structured_failure.get("failure_class")

        task_kind: str | None = None
        if isinstance(task_shape, dict):
            task_kind = task_shape.get("task_kind")

        error_signature: str | None = None
        if errors:
            for e in errors:
                stripped = e.strip()
                if stripped:
                    error_signature = stripped
                    break

        target = tuple(sorted(str(f) for f in (target_files or []) if f))
        raw = tuple(errors or [])

        created_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        record = HazardRecord(
            model=model,
            status=status,
            failure_class=failure_class,
            target_files=target,
            task_kind=task_kind,
            error_signature=error_signature,
            raw_errors=raw,
            tool_call_id=tool_call_id,
            created_at=created_at,
        )

        store = HazardStore(workspace_root / ".aura" / "hazards.db")
        try:
            row_id = store.insert(record)
        finally:
            store.close()
        return row_id
    except Exception:
        _log.exception("record_hazard failed")
        return None
