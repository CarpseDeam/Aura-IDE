"""Pre-write backups: <root>/.aura/backups/<ISO-timestamp>/<relpath>."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from aura.git_ops import ensure_aura_gitignored
from aura.paths import safe_relative_to


def _ts() -> str:
    # Filesystem-safe ISO-ish timestamp.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")


def backup_timestamp() -> str:
    """A new filesystem-safe timestamp, for grouping a multi-file backup set."""
    return _ts()


def backup_existing(workspace_root: Path, target: Path, ts: str | None = None) -> Path | None:
    """If target exists, copy it under .aura/backups/<ts>/<relpath>. Return new path.

    Pass a shared ``ts`` (from :func:`backup_timestamp`) to group several
    files backed up in the same transaction under one timestamp folder.
    """
    if not target.exists() or not target.is_file():
        return None
    rel = safe_relative_to(target, workspace_root)
    dest_dir = workspace_root / ".aura" / "backups" / (ts or _ts())
    dest = dest_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    ensure_aura_gitignored(workspace_root)
    shutil.copy2(target, dest)
    return dest
