"""Multi-file ``patch_file`` transactions: proposal, approval, commit, rollback.

Owns the mechanics of turning one or more ``patch_file`` targets into a single
reviewed, all-or-rollback transaction. Replacement matching stays in
``_replacement_engine.py``/``fs_write.py``; schema definitions stay in
``schemas/write.py``; dispatch and read-only/reject-all gating stay in
``_write_mixin.py``. This module is the one place that knows how to:

* normalize the legacy ``path``/``edits`` payload and the ``files`` payload
  into the same ordered list of per-file edit specs,
* build every proposed file's content in memory (reusing
  :func:`propose_patch_file` per file, so exact/line-exact matching, Python
  syntax rejection, and expected-hash checks are not reimplemented here),
* re-verify every target against its approved snapshot immediately before
  committing,
* commit every file and roll back whatever was already written if a later
  file's replacement fails.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aura.conversation.tools.backup import backup_existing, backup_timestamp
from aura.conversation.tools.fs_write import (
    _rel_path,
    propose_patch_file,
    stale_approval_reason,
)
from aura.paths import safe_relative_to

ResolveInRoot = Callable[[str], Path]
CaptureBeforeWrite = Callable[[str], None]
AtomicWriteBytes = Callable[[Path, bytes], None]


@dataclass(frozen=True)
class FileEditSpec:
    """One target file's edits, as requested (before resolution)."""

    path: str
    edits: list[dict[str, Any]]
    expected_file_hash: str | None = None


@dataclass(frozen=True)
class FileProposal:
    """One file's fully-built in-memory proposal, ready for approval."""

    target: Path
    rel_path: str
    old_content: str
    new_content: str
    hunk_count: int
    target_snapshot: dict[str, Any]


@dataclass(frozen=True)
class PatchTransaction:
    """A fully validated, ready-to-approve multi-file patch proposal."""

    files: tuple[FileProposal, ...]
    description: str = ""


def _invalid_form(error: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "applied": False,
        "error": error,
        "failure_class": "patch_transaction_invalid_form",
    }
    payload.update(extra)
    return payload


def normalize_patch_args(
    args: dict[str, Any],
) -> tuple[list[FileEditSpec], str, dict[str, Any] | None]:
    """Validate ``patch_file`` args and normalize to an ordered edit-spec list.

    Exactly one input form is accepted: legacy ``path`` + ``edits``, or
    ``files``. Returns ``(specs, description, None)`` on success, or
    ``([], "", failure_payload)`` when the request is malformed.
    """
    path = args.get("path")
    edits = args.get("edits")
    files = args.get("files")
    description = args.get("description")
    if description is not None and not isinstance(description, str):
        return [], "", _invalid_form(
            "description must be a string when supplied", failure_class="internal_error"
        )
    description = description or ""

    has_legacy = path is not None or edits is not None
    has_files = files is not None
    if has_legacy and has_files:
        return [], "", _invalid_form(
            "patch_file accepts either path+edits or files, not both"
        )
    if not has_legacy and not has_files:
        return [], "", _invalid_form("patch_file requires either path+edits or files")

    if has_legacy:
        if not isinstance(path, str) or not path.strip():
            return [], "", _invalid_form(
                "path must be a non-empty string", failure_class="internal_error"
            )
        if not isinstance(edits, list) or not edits:
            return [], "", _invalid_form(
                "edits must be a non-empty list", failure_class="internal_error"
            )
        expected_hash = args.get("expected_file_hash")
        if expected_hash is not None and not isinstance(expected_hash, str):
            return [], "", _invalid_form(
                "expected_file_hash must be a string when supplied",
                failure_class="internal_error",
            )
        return (
            [FileEditSpec(path=path, edits=edits, expected_file_hash=expected_hash)],
            description,
            None,
        )

    if not isinstance(files, list) or not files:
        return [], "", _invalid_form("files must be a non-empty list")

    specs: list[FileEditSpec] = []
    seen_paths: set[str] = set()
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            return [], "", _invalid_form(
                "each files entry must be an object", file_index=index
            )
        entry_path = entry.get("path")
        entry_edits = entry.get("edits")
        entry_hash = entry.get("expected_file_hash")
        if not isinstance(entry_path, str) or not entry_path.strip():
            return [], "", _invalid_form(
                "each files entry requires a non-empty path", file_index=index
            )
        if not isinstance(entry_edits, list) or not entry_edits:
            return [], "", _invalid_form(
                "each files entry requires a non-empty edits list",
                path=entry_path,
                file_index=index,
            )
        if entry_hash is not None and not isinstance(entry_hash, str):
            return [], "", _invalid_form(
                "expected_file_hash must be a string when supplied",
                failure_class="internal_error",
                path=entry_path,
                file_index=index,
            )
        normalized = entry_path.strip().lstrip("/\\").replace("\\", "/")
        if normalized in seen_paths:
            return [], "", _invalid_form(
                f"duplicate target path in files: {normalized}",
                failure_class="patch_transaction_duplicate_path",
                path=normalized,
            )
        seen_paths.add(normalized)
        specs.append(
            FileEditSpec(path=entry_path, edits=entry_edits, expected_file_hash=entry_hash)
        )

    return specs, description, None


def propose_patch_transaction(
    workspace_root: Path,
    resolve_in_root: ResolveInRoot,
    specs: list[FileEditSpec],
    description: str,
) -> tuple[PatchTransaction | None, dict[str, Any] | None]:
    """Build every target file's proposal in memory. All-or-nothing.

    Reuses :func:`propose_patch_file` per target, so exact/line-exact
    matching, ambiguous/missing-hunk failure, and Python syntax rejection are
    exactly the single-file behavior. Nothing reaches disk here. The first
    file or hunk that fails aborts the whole transaction; its raw failure
    payload — including existing diagnostic candidates — is returned
    unflattened, with the failing path always present.
    """
    proposals: list[FileProposal] = []
    seen_targets: set[Path] = set()
    for spec in specs:
        try:
            target = resolve_in_root(spec.path)
        except ValueError as exc:
            return None, {
                "ok": False,
                "applied": False,
                "path": spec.path,
                "rel_path": spec.path,
                "error": str(exc),
                "failure_class": "path_error",
            }

        if target in seen_targets:
            rel = _rel_path(workspace_root, target)
            return None, {
                "ok": False,
                "applied": False,
                "path": rel,
                "rel_path": rel,
                "error": f"duplicate target path in files: {rel}",
                "failure_class": "patch_transaction_duplicate_path",
            }
        seen_targets.add(target)

        result = propose_patch_file(
            workspace_root,
            target,
            spec.edits,
            expected_file_hash=spec.expected_file_hash,
            description=description,
        )
        if not result.get("ok", False):
            # The raw fs_write.py failure payload already names this file,
            # the failing hunk_index where relevant, and carries every
            # existing diagnostic (candidates, match_tier, ...) — returned
            # as-is rather than flattened into a summary.
            failure = dict(result)
            failure.setdefault("applied", False)
            return None, failure

        proposals.append(
            FileProposal(
                target=target,
                rel_path=str(result["rel_path"]),
                old_content=str(result["old_content"]),
                new_content=str(result["new_content"]),
                hunk_count=int(result.get("hunk_count") or len(spec.edits)),
                target_snapshot=dict(result.get("_target_snapshot") or {}),
            )
        )

    return PatchTransaction(files=tuple(proposals), description=description), None


def _stale_transaction_payload(proposal: FileProposal, stale_reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "applied": False,
        "path": proposal.rel_path,
        "rel_path": proposal.rel_path,
        "error": (
            "The target changed after approval and before the write landed: "
            f"{stale_reason}. Nothing was written."
        ),
        "failure_class": "stale_approval",
        "stale_approval": True,
        "write_outcome": "not_applied_stale_approval",
        "suggested_tool": "read_file",
        "suggested_next_tool": "read_file",
        "suggested_next_action": (
            "Re-read the current file, then re-propose the change with the "
            "current content and expected_file_hash."
        ),
    }


def verify_transaction_not_stale(transaction: PatchTransaction) -> dict[str, Any] | None:
    """Re-check every target against its approved snapshot, or None if fresh.

    Called immediately after approval and before any file in the transaction
    is touched. A single stale target fails the whole transaction; nothing is
    written for any file.
    """
    for proposal in transaction.files:
        stale_reason = stale_approval_reason(proposal.target, proposal.target_snapshot)
        if stale_reason is not None:
            return _stale_transaction_payload(proposal, stale_reason)
    return None


def _rollback(
    written: list[tuple[FileProposal, bytes]],
    atomic_write_bytes: AtomicWriteBytes,
) -> bool:
    """Restore every already-written file to its original bytes.

    Returns True only if every restore succeeded.
    """
    ok = True
    for proposal, original_bytes in reversed(written):
        try:
            atomic_write_bytes(proposal.target, original_bytes)
        except OSError:
            ok = False
    return ok


def commit_patch_transaction(
    workspace_root: Path,
    transaction: PatchTransaction,
    *,
    capture_before_write: CaptureBeforeWrite,
    atomic_write_bytes: AtomicWriteBytes,
) -> dict[str, Any]:
    """Re-verify freshness, then commit every file with all-or-rollback semantics.

    On success every file is written and a structured per-file result is
    returned. If a later file's replacement fails after earlier files were
    already written, every already-written file is restored to its exact
    original bytes before the failure is reported. If that restore itself
    fails, the result says so explicitly rather than claiming a clean
    workspace.
    """
    stale = verify_transaction_not_stale(transaction)
    if stale is not None:
        return stale

    ts = backup_timestamp()
    written: list[tuple[FileProposal, bytes]] = []
    backups: dict[str, str | None] = {}

    for proposal in transaction.files:
        try:
            original_bytes = proposal.target.read_bytes()
        except OSError as exc:
            return _commit_failure_payload(proposal, str(exc), written, atomic_write_bytes)

        capture_before_write(proposal.rel_path)
        backup_path = backup_existing(workspace_root, proposal.target, ts=ts)
        backups[proposal.rel_path] = (
            safe_relative_to(backup_path, workspace_root).as_posix()
            if backup_path is not None
            else None
        )

        try:
            atomic_write_bytes(proposal.target, proposal.new_content.encode("utf-8"))
        except OSError as exc:
            return _commit_failure_payload(proposal, str(exc), written, atomic_write_bytes)

        written.append((proposal, original_bytes))

    return {
        "ok": True,
        "applied": True,
        "write_outcome": "applied",
        "file_count": len(transaction.files),
        "files": [
            {
                "path": proposal.rel_path,
                "rel_path": proposal.rel_path,
                "edit_count": proposal.hunk_count,
                "backup": backups.get(proposal.rel_path),
            }
            for proposal in transaction.files
        ],
        "description": transaction.description,
    }


def _commit_failure_payload(
    failing: FileProposal,
    error: str,
    written: list[tuple[FileProposal, bytes]],
    atomic_write_bytes: AtomicWriteBytes,
) -> dict[str, Any]:
    if not written:
        # Nothing landed yet — the very first file failed. Nothing to roll back.
        return {
            "ok": False,
            "applied": False,
            "path": failing.rel_path,
            "rel_path": failing.rel_path,
            "error": f"failed to write {failing.rel_path}: {error}",
            "failure_class": "patch_transaction_commit_failed",
            "write_outcome": "not_applied_transaction_commit_failed",
        }

    rolled_back_ok = _rollback(written, atomic_write_bytes)
    written_paths = [proposal.rel_path for proposal, _bytes in written]
    if rolled_back_ok:
        return {
            "ok": False,
            "applied": False,
            "rolled_back": True,
            "path": failing.rel_path,
            "rel_path": failing.rel_path,
            "error": (
                f"failed to write {failing.rel_path} after {len(written)} file(s) were "
                f"already applied: {error}. All applied files were rolled back to their "
                "original content."
            ),
            "failure_class": "patch_transaction_commit_failed",
            "write_outcome": "not_applied_transaction_rolled_back",
            "rolled_back_files": written_paths,
        }

    # The commit failed AND rollback of the already-applied files failed too.
    # This must never be reported as a clean "applied: false" — the workspace
    # may genuinely contain a partial transaction.
    return {
        "ok": False,
        "applied": "partial",
        "rolled_back": False,
        "path": failing.rel_path,
        "rel_path": failing.rel_path,
        "error": (
            f"failed to write {failing.rel_path} after {len(written)} file(s) were already "
            "applied, AND restoring those already-applied files failed. The workspace may "
            "contain a partially applied transaction."
        ),
        "failure_class": "transaction_rollback_failed",
        "workspace_state": "potentially_partial",
        "write_outcome": "transaction_rollback_failed",
        "attempted_written_files": written_paths,
    }
