"""Runtime owner of the turn-scoped read-only external access allowlist.

Aura reads outside the active workspace the way an ordinary coding harness
does: the user names an absolute file or directory in their own message, and
for that turn Aura's normal read tools can read exactly what was named — a
file grants that one file, a directory grants its tree. Nothing else outside
the workspace is reachable.

This object is the single owner of that authority. It holds the turn's
allowlist, resolves a raw path argument to a canonical target, decides
file-versus-directory containment, rejects traversal and symlink/junction
escapes, clears itself at every lifecycle boundary, and reports safe display
metadata. It is never passed to write, terminal, MCP, dynamic-tool, Godot
mutation, or Git handlers: its only consumers are the read and search
handlers, and everything it authorizes is read-only.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from aura.paths import safe_is_relative_to


def looks_absolute(raw: str) -> bool:
    """True for anything that names a location rather than a relative subtree.

    ``Path.is_absolute()`` alone misses Windows drive-relative and
    rooted-without-drive forms (``/etc/passwd``, ``C:foo``) that pathlib does
    not classify as absolute but that a naive join can still walk outside a
    root. Containment on the resolved path below is the actual security
    boundary; this is an explicit, early classification so an unauthorized
    absolute path fails with a clear reason rather than by accident.
    """
    return (
        Path(raw).is_absolute()
        or raw.startswith(("/", "\\"))
        or bool(re.match(r"^[A-Za-z]:", raw))
    )


def _normcase(path: Path) -> str:
    return os.path.normcase(str(path))


class ExternalReadAccess:
    """Holds and resolves against this turn's authorized external locations.

    Responsibility is limited to: hold the allowlist (files and directory
    trees), authorize/clear it, report availability and non-sensitive display
    names, and resolve a raw path argument to a canonical authorized target.
    Nothing here grants write, execution, or history access.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._files: tuple[Path, ...] = ()
        self._directories: tuple[Path, ...] = ()

    # ---- state -------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return bool(self._files or self._directories)

    @property
    def files(self) -> tuple[Path, ...]:
        """The exact files authorized for this turn."""
        return self._files

    @property
    def directories(self) -> tuple[Path, ...]:
        """The directory trees authorized for this turn."""
        return self._directories

    @property
    def display_names(self) -> tuple[str, ...]:
        """Non-sensitive display names — never the absolute paths."""
        return tuple(path.name for path in (*self._directories, *self._files))

    def set_workspace_root(self, workspace_root: Path) -> None:
        """Repoint the workspace boundary and clear the allowlist.

        Access authorized while Project A was open must never remain reachable
        once the active workspace becomes Project B.
        """
        self._workspace_root = workspace_root.resolve()
        self.clear()

    def clear(self) -> None:
        self._files = ()
        self._directories = ()

    def authorize(self, candidates: Iterable[Path]) -> tuple[Path, ...]:
        """Replace the allowlist with *candidates*, and report what took.

        Authorization is strictly turn-scoped: whatever was authorized before
        is dropped first. Each candidate is resolved once; an existing file
        authorizes that file, an existing directory authorizes that tree, and
        anything that no longer exists or cannot be resolved authorizes
        nothing. Overlapping paths are normalized rather than rejected — a
        directory plus a file inside it is an ordinary request, not an error.
        """
        self.clear()

        files: list[Path] = []
        directories: list[Path] = []
        for candidate in candidates or ():
            try:
                resolved = Path(candidate).expanduser().resolve()
                is_dir = resolved.is_dir()
                is_file = resolved.is_file()
            except (OSError, ValueError):
                continue
            if is_dir:
                directories.append(resolved)
            elif is_file:
                files.append(resolved)

        self._directories = _dedupe_directories(directories)
        # A file already covered by an authorized tree adds nothing.
        self._files = tuple(
            path for path in _dedupe(files)
            if not any(safe_is_relative_to(path, root) for root in self._directories)
        )
        return (*self._directories, *self._files)

    # ---- resolution --------------------------------------------------------

    def allows(self, resolved: Path) -> bool:
        """Whether an already-canonical path is inside the allowlist."""
        if not self.is_available:
            return False
        target = _normcase(resolved)
        if any(target == _normcase(path) for path in self._files):
            return True
        return any(safe_is_relative_to(resolved, root) for root in self._directories)

    def resolve(self, raw: str) -> Path:
        """Resolve an absolute read target, or raise ``ValueError``.

        The returned path is canonical: symlinks and junctions are followed
        *before* containment is checked, so a link inside an authorized
        directory cannot smuggle in what it points at.
        """
        if raw is None:
            raise ValueError("path is required")
        text = str(raw).strip().strip('"').strip("`")
        if text == "":
            raise ValueError("path must not be empty")
        if ".." in Path(text).parts:
            raise ValueError("'..' is not allowed in tool paths")
        if not looks_absolute(text):
            raise ValueError(f"path '{raw}' is not an absolute path")

        try:
            resolved = Path(text).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise ValueError(f"path '{raw}' could not be resolved: {exc}") from exc

        if not self.allows(resolved):
            raise ValueError(
                f"path '{raw}' is outside the workspace and was not authorized "
                "for this turn; only absolute paths the user named in their own "
                "message can be read outside the workspace"
            )
        return resolved


def _dedupe(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = _normcase(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


def _dedupe_directories(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Drop directories already covered by another authorized directory."""
    unique = _dedupe(paths)
    kept: list[Path] = []
    for path in unique:
        covered = any(
            other is not path and safe_is_relative_to(path, other) for other in unique
        )
        if not covered:
            kept.append(path)
    return tuple(kept)
