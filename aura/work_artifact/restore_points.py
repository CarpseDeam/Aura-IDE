"""Restore point substrate for WorkArtifact item rollback.

Phase 1 — capture only, no restore logic.

Capture happens before file mutation. Each session is keyed by
(artifact_id, item_id) and stores pre-write state as blobs under
``.aura/restore_points/<artifact_id>/<item_id>/``.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# ── Public types ──────────────────────────────────────────────────────────


class BaselineState(enum.Enum):
    """Pre-write state of a file under a restore point session.

    Attributes:
        present: File existed and its bytes were captured.
        absent: File did not exist (creation could be rolled back by deletion).
        ignored: Path matched an ignore rule; no state captured.
        out_of_bounds: Path was rejected (traversal or outside workspace).
    """

    present = "present"
    absent = "absent"
    ignored = "ignored"
    out_of_bounds = "out_of_bounds"


@dataclass(frozen=True)
class CaptureResult:
    """Result of a single ``capture_pre_write`` call.

    Attributes:
        state: The BaselineState determined for the path.
        already_captured: True when the path was already captured in this
            session (idempotent).
        blob_path: Relative path to the blob file under the session's
            blobs/ directory, or empty string if no blob was stored.
        digest: SHA-256 hex digest of captured bytes (or empty string for
            non-present states).
        reason: Human-readable detail (e.g. which ignore rule matched).
    """

    state: BaselineState
    already_captured: bool = False
    blob_path: str = ""
    digest: str = ""
    reason: str = ""


# ── Ignore logic ─────────────────────────────────────────────────────────-

_IGNORE_COMPONENTS: set[str] = {
    ".git",
    ".aura",
    "venv",
    ".venv",
    "build",
    "cache",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

# Directories whose *contents* should be ignored (but the directory entry
# itself is a no-op rather than an error).  Only the directory leaf component
# is matched so that e.g. ``subdir/build/`` is ignored while
# ``src/build_utils.py`` is not.
_IGNORE_DIR_LEAVES: set[str] = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}


def _path_is_ignored(rel_path: Path, workspace_root: Path) -> tuple[bool, str]:
    """Return (True, reason) if *rel_path* should be ignored.

    Checks:
      - Any component matching a known-ignore directory leaf name
        (``__pycache__``, ``.mypy_cache``, ``.pytest_cache``,
        ``.ruff_cache``, ``node_modules``).
      - Top-level .git/ and .aura/ directories.
      - Top-level venv/ and build/ directories (common build artifacts).
    """
    # Normalise to forward-slash string for matching.
    parts = rel_path.parts

    # Ignore any path whose leaf component matches a cache/product directory.
    for part in parts:
        if part in _IGNORE_DIR_LEAVES:
            return True, f"ignore: path component '{part}' matched ignore rule"

    # Top-level entries: check the first component.
    if parts:
        top = parts[0]
        if top == ".git":
            return True, "ignore: .git directory"
        if top == ".aura":
            return True, "ignore: .aura directory"
        if top in ("venv", ".venv"):
            return True, "ignore: venv directory"
        if top == "build":
            return True, "ignore: build directory"

    return False, ""


def _path_is_out_of_bounds(
    rel_path: Path, workspace_root: Path
) -> tuple[bool, str]:
    """Return (True, reason) if *rel_path* escapes the workspace.

    Checks:
      - Absolute paths.
      - Paths containing ``..`` traversal.
      - Symlinks that resolve outside the workspace (checked via
        ``resolve()`` comparison).
    """
    if rel_path.is_absolute():
        return True, "out_of_bounds: absolute path not allowed"

    if ".." in rel_path.parts:
        return True, "out_of_bounds: path traversal detected"

    # If the resolved path escapes the workspace root, reject.
    try:
        resolved = (workspace_root / rel_path).resolve()
    except (ValueError, OSError):
        return True, "out_of_bounds: path resolution error"

    ws_resolved = workspace_root.resolve()
    if not str(resolved).startswith(str(ws_resolved)):
        return True, "out_of_bounds: symlink escape"

    return False, ""


# ── Storage helpers ───────────────────────────────────────────────────────

import re as _re


def _make_storage_key(raw: str) -> str:
    """Convert an arbitrary identifier to a filesystem-safe storage key.

    Format: ``<slug>-<sha256_prefix>``

    The slug preserves readable characters for debugging.  The SHA-256
    prefix guarantees uniqueness and makes the key stable across sessions.
    The original ID is stored in the session manifest for reference.

    Raises:
        ValueError: If *raw* is empty or whitespace-only.
    """
    if not raw or not raw.strip():
        raise ValueError(f"empty or whitespace-only ID: {raw!r}")

    s = raw.strip().lower()

    # Replace path separators and drive-letter colon.
    s = _re.sub(r"[/\\:]+", "_", s)

    # Remove control characters.
    s = _re.sub(r"[\x00-\x1f\x7f]", "", s)

    # Replace anything that is not a word character, dot, or dash.
    s = _re.sub(r"[^\w.\-]", "_", s)

    # Collapse consecutive dots (prevents ``..`` traversal in component).
    s = _re.sub(r"\.{2,}", "_", s)

    # Collapse consecutive underscores.
    s = _re.sub(r"_+", "_", s)

    # Strip leading/trailing special characters.
    s = s.strip("_.-")

    # Fallback if nothing survived slugification.
    if not s:
        s = "id"

    # Keep the slug readable but bounded.
    max_slug = 48
    if len(s) > max_slug:
        s = s[:max_slug].rstrip("_.-")

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{s}-{digest}"


def _session_storage_dir(
    workspace_root: Path,
    artifact_id: str,
    item_id: str,
) -> Path:
    """Return the filesystem-safe restore-point directory for an (artifact, item) pair."""
    return (
        workspace_root
        / ".aura"
        / "restore_points"
        / _make_storage_key(artifact_id)
        / _make_storage_key(item_id)
    )


def _artifact_storage_dir(
    workspace_root: Path,
    artifact_id: str,
) -> Path:
    """Return the filesystem-safe restore-point directory for an artifact."""
    return (
        workspace_root
        / ".aura"
        / "restore_points"
        / _make_storage_key(artifact_id)
    )


def _blob_dir(session_dir: Path) -> Path:
    """Return the blobs subdirectory under a session directory."""
    return session_dir / "blobs"


def _blob_path(session_dir: Path, digest: str) -> Path:
    """Return the blob file path for a given digest."""
    return _blob_dir(session_dir) / f"{digest}.bin"


# ── RestorePointSession ───────────────────────────────────────────────────


class RestorePointSession:
    """Captures pre-write state for one (artifact_id, item_id) pair.

    One session per ``(artifact_id, item_id)``.  ``capture_pre_write`` is
    idempotent: the first call stores state; subsequent calls for the same
    path return ``already_captured=True``.

    Attributes:
        artifact_id: The artifact this session belongs to.
        item_id: The work item this session belongs to.
        session_dir: The on-disk directory for this session's data.
        manifest: Dict mapping relative path -> capture metadata.
    """

    def __init__(
        self,
        artifact_id: str,
        item_id: str,
        workspace_root: Path,
    ) -> None:
        self.artifact_id = artifact_id
        self.item_id = item_id
        self._workspace_root = workspace_root.resolve()
        self._captured: set[str] = set()  # normalised forward-slash paths
        self.manifest: dict[str, dict[str, Any]] = {}
        self._dirty: bool = False

        # Set up session directory using filesystem-safe storage keys.
        self.session_dir = _session_storage_dir(
            self._workspace_root, artifact_id, item_id
        )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        _blob_dir(self.session_dir).mkdir(parents=True, exist_ok=True)

        # Load existing manifest if present (e.g. from a previous partial run).
        manifest_path = self.session_dir / "manifest.json"
        if manifest_path.exists():
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for path_key, entry in raw.items():
                        if isinstance(entry, dict):
                            self.manifest[path_key] = entry
                            self._captured.add(path_key)
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning(
                    "Corrupt manifest %s: %s; will overwrite",
                    manifest_path,
                    exc,
                )

    # ── public API ───────────────────────────────────────────────────────

    def capture_pre_write(self, rel_path: str) -> CaptureResult:
        """Capture the pre-write state of *rel_path*.

        Args:
            rel_path: Workspace-relative path (forward or native slashes).

        Returns:
            A ``CaptureResult`` describing what was captured (or why not).
        """
        path = Path(rel_path).resolve() if Path(rel_path).is_absolute() else Path(rel_path)

        # 1. Out-of-bounds check (before normalised key construction).
        is_oob, oob_reason = _path_is_out_of_bounds(path, self._workspace_root)
        if is_oob:
            return CaptureResult(
                state=BaselineState.out_of_bounds,
                reason=oob_reason,
            )

        # Normalise key: relative to workspace root for manifest.
        try:
            rel_key = str(path.relative_to(self._workspace_root).as_posix())
        except ValueError:
            # Fall back to the original relative path.
            rel_key = path.as_posix()

        # 2. Already captured?
        if rel_key in self._captured:
            entry = self.manifest.get(rel_key, {})
            return CaptureResult(
                state=BaselineState(entry.get("state", "present")),
                already_captured=True,
                blob_path=entry.get("blob_path", ""),
                digest=entry.get("digest", ""),
                reason="already captured",
            )

        # 3. Ignore check.
        is_ignored, ignore_reason = _path_is_ignored(path, self._workspace_root)
        if is_ignored:
            self._record_manifest(rel_key, BaselineState.ignored, reason=ignore_reason)
            return CaptureResult(
                state=BaselineState.ignored,
                reason=ignore_reason,
            )

        full_path = self._workspace_root / rel_key

        # 4. Missing file.
        if not full_path.exists() and not full_path.is_symlink():
            self._record_manifest(rel_key, BaselineState.absent, reason="file does not exist")
            return CaptureResult(
                state=BaselineState.absent,
                reason="file does not exist",
            )

        # 5. Existing file — capture bytes as blob.
        try:
            data = full_path.read_bytes()
        except OSError as exc:
            _log.warning("Failed to read %s: %s", full_path, exc)
            return CaptureResult(
                state=BaselineState.absent,
                reason=f"read error: {exc}",
            )

        digest = hashlib.sha256(data).hexdigest()
        blob = _blob_path(self.session_dir, digest)
        blob.write_bytes(data)

        # Store blob path relative to the session directory.
        blob_rel_key = f"blobs/{digest}.bin"

        self._record_manifest(
            rel_key,
            BaselineState.present,
            blob_path=blob_rel_key,
            digest=digest,
        )

        return CaptureResult(
            state=BaselineState.present,
            blob_path=blob_rel_key,
            digest=digest,
            reason="captured",
        )

    def save_manifest(self) -> None:
        """Write the current manifest to disk as JSON.

        This is safe to call multiple times; the manifest is only written
        when it has changed since the last write or load.
        """
        if not self._dirty:
            return
        manifest_path = self.session_dir / "manifest.json"
        try:
            manifest_path.write_text(
                json.dumps(self.manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self._dirty = False
        except OSError as exc:
            _log.error("Failed to write manifest %s: %s", manifest_path, exc)

    def close(self) -> None:
        """Flush manifest to disk and release resources."""
        self.save_manifest()

    @property
    def captured_paths(self) -> list[str]:
        """Return the list of captured path keys (forward-slash)."""
        return sorted(self._captured)

    # ── internal helpers ─────────────────────────────────────────────────

    def _record_manifest(
        self,
        rel_key: str,
        state: BaselineState,
        *,
        blob_path: str = "",
        digest: str = "",
        reason: str = "",
    ) -> None:
        """Record an entry in the in-memory manifest."""
        entry: dict[str, Any] = {
            "state": state.value,
        }
        if blob_path:
            entry["blob_path"] = blob_path
        if digest:
            entry["digest"] = digest
        if reason:
            entry["reason"] = reason
        self.manifest[rel_key] = entry
        self._captured.add(rel_key)
        self._dirty = True


# ── RestorePointManager ───────────────────────────────────────────────────


class RestorePointManager:
    """Manages restore point sessions keyed by ``(artifact_id, item_id)``.

    Owned by ``_DispatchProxy``.  Call ``open_session`` to start capturing
    pre-write state for a work item, and ``close_session`` when the item
    completes.
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._workspace_root = (
            workspace_root.resolve() if workspace_root else Path.cwd().resolve()
        )
        self._sessions: dict[tuple[str, str], RestorePointSession] = {}

    # ── public API ───────────────────────────────────────────────────────

    def open_session(
        self, artifact_id: str, item_id: str
    ) -> RestorePointSession:
        """Open (or return existing) session for *(artifact_id, item_id)*.

        The session directory is
        ``.aura/restore_points/<artifact_id>/<item_id>/``.
        """
        key = (artifact_id, item_id)
        existing = self._sessions.get(key)
        if existing is not None:
            return existing

        session = RestorePointSession(
            artifact_id=artifact_id,
            item_id=item_id,
            workspace_root=self._workspace_root,
        )
        self._sessions[key] = session
        _log.info(
            "RestorePointSession opened artifact_id=%s item_id=%s dir=%s",
            artifact_id,
            item_id,
            session.session_dir,
        )
        return session

    def close_session(self, artifact_id: str, item_id: str) -> None:
        """Close and flush the session for *(artifact_id, item_id)*."""
        key = (artifact_id, item_id)
        session = self._sessions.pop(key, None)
        if session is not None:
            session.close()
            _log.info(
                "RestorePointSession closed artifact_id=%s item_id=%s",
                artifact_id,
                item_id,
            )

    def close_all(self) -> None:
        """Close and flush all open sessions."""
        for key in list(self._sessions):
            self.close_session(*key)

    def get_session(
        self, artifact_id: str, item_id: str
    ) -> RestorePointSession | None:
        """Return the open session for *(artifact_id, item_id)*, or None."""
        return self._sessions.get((artifact_id, item_id))

    @property
    def workspace_root(self) -> Path:
        """The workspace root this manager was bound to."""
        return self._workspace_root

    @property
    def open_count(self) -> int:
        """Number of currently open sessions."""
        return len(self._sessions)

    def capture_path(self, rel_path: str) -> list[CaptureResult]:
        """Capture *rel_path* across all currently open sessions.

        This is the intended integration point for the write-tool layer:
        before a file mutation, call this once so every open session
        captures its baseline.  Idempotent per-session — paths already
        captured are returned with ``already_captured=True``.

        Returns a list of ``CaptureResult``, one per open session, or an
        empty list when no sessions are open.
        """
        return [
            session.capture_pre_write(rel_path)
            for session in self._sessions.values()
        ]

    # ── cleanup ──────────────────────────────────────────────────────────

    def delete_session_storage(
        self, artifact_id: str, item_id: str
    ) -> None:
        """Remove the on-disk restore point directory for a completed item.

        Use this when an item succeeds and restore points are no longer
        needed.  The in-memory session (if open) is closed first.
        """
        self.close_session(artifact_id, item_id)
        session_dir = _session_storage_dir(
            self._workspace_root, artifact_id, item_id
        )
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            _log.info(
                "Deleted restore point storage artifact_id=%s item_id=%s",
                artifact_id,
                item_id,
            )

    def delete_artifact_storage(self, artifact_id: str) -> None:
        """Remove all restore point data for an entire artifact."""
        # Close any open sessions for this artifact.
        for (aid, iid) in list(self._sessions):
            if aid == artifact_id:
                self.close_session(aid, iid)
        artifact_dir = _artifact_storage_dir(
            self._workspace_root, artifact_id
        )
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)
            _log.info(
                "Deleted artifact restore point storage artifact_id=%s",
                artifact_id,
            )
