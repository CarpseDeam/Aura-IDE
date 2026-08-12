"""Runtime owner of the single turn-scoped read-only external reference.

Holds at most one authorized external root, resolves reference-relative paths
against it, and enforces that the resolved target never escapes it. This object
is never passed to write,
terminal, MCP, dynamic-tool, Godot mutation, or Git handlers — its only
consumer is the read_reference_file handler.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from aura.paths import safe_is_relative_to


def _looks_absolute(raw: str) -> bool:
    """True for anything that names a location outside a relative subtree.

    ``Path.is_absolute()`` alone misses Windows drive-relative and
    rooted-without-drive forms (``/etc/passwd``, ``C:foo``) that pathlib does
    not classify as absolute but that a naive join can still walk outside the
    reference root. The final ``safe_is_relative_to`` containment check below
    is the actual security boundary; this is an explicit, early rejection so
    the error is clear rather than accidental.
    """
    return (
        Path(raw).is_absolute()
        or raw.startswith(("/", "\\"))
        or bool(re.match(r"^[A-Za-z]:", raw))
    )


class ReferenceRootAccess:
    """Holds and resolves against the current turn's external reference.

    Responsibility is limited to: hold the authorized root (or None),
    attach/clear it, report availability and a non-sensitive display name,
    and resolve a reference-relative path safely. Nothing here grants
    write, execution, or history access to the reference.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._root: Path | None = None

    @property
    def is_available(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def name(self) -> str | None:
        """Non-sensitive display name — never the absolute path."""
        return self._root.name if self._root is not None else None

    def set_workspace_root(self, workspace_root: Path) -> None:
        """Repoint the workspace boundary and clear any attached reference.

        A reference authorized for the old workspace must never remain
        reachable once the active workspace becomes Project B.
        """
        self._workspace_root = workspace_root.resolve()
        self._root = None

    def attach(self, candidate: Path) -> tuple[bool, str]:
        """Attempt to authorize *candidate* as the Reference Folder.

        Returns ``(ok, message)``. On success the root is now attached; on
        failure the previously attached root (if any) is left unchanged.
        """
        if not _looks_absolute(str(candidate)):
            return False, "Reference Folder path must be absolute"

        try:
            resolved = candidate.resolve()
        except OSError as exc:
            return False, f"could not resolve path: {exc}"

        if not resolved.exists() or not resolved.is_dir():
            return False, "Reference Folder must be an existing directory"

        broad_root_error = self._broad_root_error(resolved)
        if broad_root_error is not None:
            return False, broad_root_error

        if resolved == self._workspace_root:
            return False, "Reference Folder cannot be the active workspace itself"

        if safe_is_relative_to(resolved, self._workspace_root):
            return False, "Reference Folder cannot be inside the active workspace"

        if safe_is_relative_to(self._workspace_root, resolved):
            return False, "Reference Folder cannot contain the active workspace"

        self._root = resolved
        return True, "attached"

    @staticmethod
    def _broad_root_error(candidate: Path) -> str | None:
        """Reject roots whose scope is obviously broader than one project."""
        if candidate.parent == candidate:
            return "Reference Folder cannot be a filesystem or drive root"

        try:
            home = Path.home().resolve()
        except (OSError, RuntimeError):
            home = None

        if home is None:
            return None

        def same_path(left: Path, right: Path) -> bool:
            return os.path.normcase(str(left)) == os.path.normcase(str(right))

        if same_path(candidate, home):
            return "Reference Folder cannot be the user home directory"

        broad_names = ("Desktop", "Documents", "Downloads", "OneDrive")
        if any(same_path(candidate, (home / name).resolve()) for name in broad_names):
            return "Reference Folder cannot be a broad user folder"
        return None

    def clear(self) -> None:
        self._root = None

    def resolve(self, raw: str) -> Path:
        """Resolve a reference-relative path, or raise ``ValueError``."""
        if self._root is None:
            raise ValueError("no Reference Folder is attached")
        if raw is None:
            raise ValueError("path is required")
        s = str(raw).strip()
        if s == "":
            raise ValueError("path must not be empty")
        if _looks_absolute(s):
            raise ValueError("path must be relative to the Reference Folder")
        if ".." in Path(s).parts:
            raise ValueError("'..' is not allowed in reference paths")
        candidate = (self._root / s).resolve()
        if not safe_is_relative_to(candidate, self._root):
            raise ValueError(f"path '{raw}' escapes the Reference Folder")
        return candidate
