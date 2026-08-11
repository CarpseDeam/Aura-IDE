"""Shared filesystem utilities.

Repository-wide candidate discovery, traversal skip policy, and sensitive-file
policy are owned by :mod:`aura.repository_inventory`. This module re-exports
the skip sets for :mod:`aura.dep_graph` (a legacy compatibility shim that
walks the tree itself) and keeps :func:`get_max_mtime` working for the same
caller, now implemented on top of the canonical inventory instead of its own
traversal.
"""

from __future__ import annotations

from pathlib import Path

from aura.repository_inventory import (
    SKIP_DIRS,  # noqa: F401 — re-exported for aura.dep_graph
    SKIP_FILE_SUFFIXES,  # noqa: F401 — re-exported for aura.dep_graph
    build_inventory,
)


def get_max_mtime(root: Path) -> float:
    """Return the maximum mtime across the canonical repository candidate files.

    Delegates discovery to :func:`aura.repository_inventory.build_inventory`
    rather than duplicating traversal rules. Used by :mod:`aura.dep_graph` for
    its own cache-staleness check.
    """
    inventory = build_inventory(root)
    if not inventory.files:
        return 0.0
    return max(f.mtime for f in inventory.files)
