"""Canonical repository-file inventory: one owner for candidate discovery.

Several repository-aware subsystems — :class:`~aura.codebase_index.indexer.CodebaseIndex`
(BM25), :class:`~aura.code_intel.index.CodeIntelIndex`, and :mod:`aura.repo_map`'s
cache-freshness check — each used to decide independently what counts as "a
repository file": different skip rules, different traversal logic, different
sensitive-file handling. This module is the single owner of that decision.

It owns:

* discovering the repository's candidate file universe (Git-aware when
  possible, a bounded filesystem walk otherwise)
* workspace-relative canonical (POSIX) paths, jailed to the workspace root
* the shared skip-directory / skip-suffix / hidden-file traversal policy
* sensitive-file (credential material) exclusion
* basic stat metadata (size, mtime) captured once during discovery
* truthful partial/truncated discovery state

It does **not** own indexing, parsing, or search suitability. A file the
inventory reports may still be too large for CodeIntel to parse, or unsuited
to BM25's own acceptance rules — those are consumer-specific capability
decisions that stay with the consumer. See :data:`RepositoryInventory` for the
snapshot contract consumers build on.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from aura.config import SKIP_DIRS, SKIP_FILE_SUFFIXES, get_subprocess_kwargs
from aura.git_ops import is_git_repo
from aura.paths import safe_is_relative_to

__all__ = [
    "SKIP_DIRS",
    "SKIP_FILE_SUFFIXES",
    "RepositoryFile",
    "RepositoryInventory",
    "is_sensitive_path",
    "passes_canonical_policy",
    "build_inventory",
]

#: Wall-clock ceiling for one `git ls-files` invocation.
_GIT_DISCOVERY_TIMEOUT_SECONDS = 15.0

#: Bounds for the filesystem-fallback walk (no Git, or Git unusable).
MAX_DIRS_VISITED = 2000
MAX_FILES_CONSIDERED = 5000
MAX_SCAN_SECONDS = 5.0


# ---- sensitive-file policy --------------------------------------------------
#
# Protects broad textual discovery (grep_search, this inventory, and anything
# built on it) from surfacing credential material. Originally owned by
# aura.conversation.tools.search_scope; moved here so both grep and every
# repository index share one authority. search_scope re-exports
# ``is_sensitive_path`` rather than keeping its own copy.

#: Exact file names that hold credentials by convention.
_SENSITIVE_NAMES: frozenset[str] = frozenset({
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    "credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
})

#: Suffixes of key/certificate material.
_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p8",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".asc",
    ".gpg",
    ".kdbx",
)

#: Directories whose contents are credential stores wherever they appear.
_SENSITIVE_DIRS: frozenset[str] = frozenset({
    ".ssh",
    ".gnupg",
    ".aws",
})

#: Aura's own on-disk secrets. ``.aura`` is already in ``SKIP_DIRS`` for the
#: walkers, but the policy states it independently so it holds for any caller.
_SENSITIVE_PREFIXES: tuple[str, ...] = (
    ".aura/secrets/",
    ".aura/tokens/",
    ".aura/config/",
)

#: ``.env`` variants that are checked-in templates, not real secrets.
_ENV_TEMPLATE_SUFFIXES: tuple[str, ...] = (
    ".example",
    ".sample",
    ".template",
    ".dist",
)


def is_sensitive_path(rel_path: str | Path) -> bool:
    """True when *rel_path* is credential material that must not be indexed.

    *rel_path* is workspace-relative; the check is on names and directories,
    never on content, so it costs nothing and cannot be defeated by a file
    being unreadable or binary.
    """
    text = str(rel_path).replace("\\", "/").lstrip("/")
    if not text:
        return False
    posix = PurePosixPath(text)
    name = posix.name
    lowered = name.lower()

    if lowered.startswith(".env"):
        # `.env.example` and friends are templates committed on purpose.
        return not lowered.endswith(_ENV_TEMPLATE_SUFFIXES)

    if lowered in _SENSITIVE_NAMES:
        return True

    # `id_ed25519` is a private key; `id_ed25519.pub` is not.
    if lowered.endswith(".pub"):
        return False

    if lowered.endswith(_SENSITIVE_SUFFIXES):
        return True

    if _SENSITIVE_DIRS & {part.lower() for part in posix.parts[:-1]}:
        return True

    return text.startswith(_SENSITIVE_PREFIXES)


# ---- canonical skip policy ---------------------------------------------------


def passes_canonical_policy(rel_path: str) -> bool:
    """True when *rel_path* is a repository candidate under canonical policy.

    Applies the shared skip-dir / skip-suffix / hidden-file / sensitive-file
    rules to a single workspace-relative path without touching the workspace
    as a whole. This is the cheap, single-path check a targeted freshness
    operation (e.g. ``CodeIntelIndex.ensure_fresh``) needs instead of a full
    inventory scan.
    """
    text = rel_path.replace("\\", "/").lstrip("/")
    if not text:
        return False
    posix = PurePosixPath(text)

    for part in posix.parts[:-1]:
        if part in SKIP_DIRS or part.startswith("."):
            return False

    name = posix.name
    if name.startswith("."):
        return False
    if posix.suffix.lower() in SKIP_FILE_SUFFIXES:
        return False
    if is_sensitive_path(text):
        return False
    return True


# ---- inventory model -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    """One repository candidate file: canonical path plus stat metadata.

    ``abs_path`` is already resolved and verified to sit inside the workspace
    root that produced this record — a symlink escaping the workspace never
    reaches this point.
    """

    rel_path: str
    abs_path: Path
    size: int
    mtime: float


@dataclass(frozen=True, slots=True)
class RepositoryInventory:
    """A snapshot of the repository's candidate file universe.

    ``complete`` is False whenever discovery stopped short of the true
    repository contents — a filesystem traversal budget was exhausted, or Git
    discovery failed and a partial fallback ran. Consumers that would evict
    cached state for files they no longer "see" must check ``complete``
    first: an incomplete snapshot's silence about a path means nothing.
    """

    root: Path
    files: tuple[RepositoryFile, ...]
    source: str  # "git" | "filesystem"
    complete: bool
    incomplete_reason: str | None = None

    @property
    def file_count(self) -> int:
        return len(self.files)

    def rel_paths(self) -> list[str]:
        return [f.rel_path for f in self.files]


# ---- discovery -------------------------------------------------------------


def build_inventory(root: Path) -> RepositoryInventory:
    """Discover the canonical repository candidate file universe under *root*.

    Prefers Git-aware discovery (tracked files plus non-ignored untracked
    files, honoring ``.gitignore`` / ``.git/info/exclude`` / standard
    excludes) when *root* is a Git working tree and the ``git`` executable is
    usable. Falls back to a bounded filesystem walk otherwise, or when the
    Git command itself fails.

    Every returned file has passed the canonical skip/sensitive policy and
    been verified to resolve inside *root* — a symlink escaping the workspace
    is never returned, regardless of what Git or the filesystem walk reports.
    """
    root_resolved = root.resolve()

    if is_git_repo(root_resolved):
        git_rel_paths = _git_ls_files(root_resolved)
        if git_rel_paths is not None:
            return _build_from_candidates(root_resolved, git_rel_paths, source="git")
        # Git is present and this is a work tree, but the command itself
        # failed (permissions, corrupt index, etc.) — fall back, but say so.
        fallback = _build_from_filesystem(root_resolved)
        reason = fallback.incomplete_reason or "git ls-files failed; used filesystem fallback"
        return replace(fallback, complete=False, incomplete_reason=reason)

    return _build_from_filesystem(root_resolved)


def _git_ls_files(root: Path) -> list[str] | None:
    """Return NUL-delimited relative paths from Git, or None if unusable."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=str(root),
            capture_output=True,
            timeout=_GIT_DISCOVERY_TIMEOUT_SECONDS,
            **get_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    raw = result.stdout or b""
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    return [p for p in text.split("\x00") if p]


def _resolve_candidate(root: Path, posix_rel: str) -> RepositoryFile | None:
    """Stat and jail-check one candidate path; None if it must be rejected."""
    abs_path = root / posix_rel
    try:
        resolved = abs_path.resolve()
    except OSError:
        return None
    if not safe_is_relative_to(resolved, root):
        return None  # symlink (or similar) escapes the workspace
    try:
        st = resolved.stat()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    return RepositoryFile(rel_path=posix_rel, abs_path=resolved, size=st.st_size, mtime=st.st_mtime)


def _build_from_candidates(root: Path, rel_paths: list[str], *, source: str) -> RepositoryInventory:
    files: list[RepositoryFile] = []
    for raw_rel in rel_paths:
        posix_rel = raw_rel.replace("\\", "/")
        if not passes_canonical_policy(posix_rel):
            continue
        rf = _resolve_candidate(root, posix_rel)
        if rf is not None:
            files.append(rf)
    return RepositoryInventory(root=root, files=tuple(files), source=source, complete=True)


def _build_from_filesystem(root: Path) -> RepositoryInventory:
    """Bounded os.walk discovery, truthful about truncation."""
    files: list[RepositoryFile] = []
    dirs_visited = 0
    files_considered = 0
    start = time.monotonic()
    incomplete_reason: str | None = None

    for dirpath, dirnames, filenames in os.walk(root):
        dirs_visited += 1
        if dirs_visited > MAX_DIRS_VISITED:
            incomplete_reason = "directory cap reached"
            break
        if time.monotonic() - start > MAX_SCAN_SECONDS:
            incomplete_reason = "scan timeout"
            break

        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))

        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""

        budget_exceeded = False
        for fname in sorted(filenames):
            if files_considered >= MAX_FILES_CONSIDERED:
                incomplete_reason = "file cap reached"
                budget_exceeded = True
                break
            files_considered += 1

            if fname.startswith("."):
                continue
            if Path(fname).suffix.lower() in SKIP_FILE_SUFFIXES:
                continue

            posix_rel = (f"{rel_dir}/{fname}" if rel_dir else fname).replace("\\", "/")
            if is_sensitive_path(posix_rel):
                continue

            rf = _resolve_candidate(root, posix_rel)
            if rf is not None:
                files.append(rf)

        if budget_exceeded:
            break

    return RepositoryInventory(
        root=root,
        files=tuple(files),
        source="filesystem",
        complete=incomplete_reason is None,
        incomplete_reason=incomplete_reason,
    )
