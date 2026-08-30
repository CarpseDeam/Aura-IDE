"""Git worktree lifecycle for writable delegated agents.

``AgentWorktreeManager`` is the only owner of writable child state.  A run is
created from a clean primary worktree at an exact commit, checkpointed as one
runtime commit, inspected without touching the primary checkout, and either
fast-forwarded into that checkout or explicitly discarded.

The linked worktree is isolation for Git changes, not an operating-system
sandbox.  In particular, a terminal-enabled child still runs with the user's
authority and can reach absolute paths, the network, credentials, and Git's
shared metadata.

Only the rules live here.  Running Git and turning a Git failure into a
reportable :class:`~aura.agents.worktree_git.AgentWorktreeError` belongs to
:class:`~aura.agents.worktree_git.GitRunner`, which this manager reaches
through for every call it makes.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from aura.agents.worktree_checkpoint import WorktreeCheckpointer
from aura.agents.worktree_creation import WorktreeCreator
from aura.agents.worktree_git import AgentWorktreeError, GitRunner
from aura.agents.worktree_inspection import ChangeSetInspector
from aura.agents.worktree_material import ChangeSetMaterializer
from aura.agents.worktree_models import AgentChangeSet, AgentWorktree
from aura.agents.worktree_operations import WorktreeGitOperations
from aura.agents.worktree_records import WorktreeRecord, WorktreeRecordStore
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalRequest,
)
from aura.paths import data_dir, safe_is_relative_to

_BRANCH_PREFIX = "refs/heads/aura/agent/"


_Record = WorktreeRecord


class AgentWorktreeManager:
    """Sole owner of writable Agent creation, results, and cleanup."""

    def __init__(
        self,
        workspace_root: Path | str | None,
        *,
        runtime_root: Path | str | None = None,
    ) -> None:
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root is not None else None
        )
        self._runtime_base = Path(runtime_root) if runtime_root is not None else data_dir() / "aw"
        self._record_store = WorktreeRecordStore(
            self._runtime_base, self._workspace_root
        )
        # Every Git call this manager makes goes through here. It owns running
        # the process and naming a failure; this class owns the rules about
        # when a call is allowed to happen at all.
        self._git = GitRunner()
        self._ops = WorktreeGitOperations(self._git, branch_prefix=_BRANCH_PREFIX)
        self._creator = WorktreeCreator(
            self._git, self._ops, branch_prefix=_BRANCH_PREFIX
        )
        self._material = ChangeSetMaterializer(self._git)
        self._inspector = ChangeSetInspector(self._git, self._material)
        self._checkpointer = WorktreeCheckpointer(self._git, self._material)
        self._lock = threading.RLock()
        self._active_id = ""
        self._pending_workspace_root: Path | None = None
        self._pending_workspace_change = False
        self._records: dict[str, _Record] = {}
        self._reload()

    @property
    def workspace_root(self) -> Path | None:
        return self._workspace_root

    @property
    def has_unresolved(self) -> bool:
        return bool(self._records)

    def set_workspace_root(self, root: Path | str | None) -> None:
        with self._lock:
            if self._active_id:
                # Workspace switches first cancel the active turn. Keep this
                # manager bound to the old repository until cancellation has
                # stopped the child and checkpointed recovery, then rebind.
                self._pending_workspace_root = (
                    Path(root).resolve() if root is not None else None
                )
                self._pending_workspace_change = True
                return
            self._rebind(root)

    # ---- creation -----------------------------------------------------

    def create(self, agent_id: str) -> AgentWorktree:
        """Create a unique Aura-owned branch and linked worktree at exact HEAD."""
        with self._lock:
            change_set_id = f"aw-{uuid.uuid4().hex[:16]}"
            if self._active_id:
                raise AgentWorktreeError(
                    "writable_delegation_busy",
                    "Another writable agent is already running.",
                    change_set_id=change_set_id,
                )
            root = self._require_repository(change_set_id)
            scope = self._scope_dir(root)
            record = self._creator.prepare(
                workspace_root=root,
                scope=scope,
                change_set_id=change_set_id,
                agent_id=agent_id,
            )
            self._records[change_set_id] = record
            try:
                self._save()
            except Exception:
                # No Git artifact exists yet. Do not let a failed journal
                # write leave an in-memory record occupying future work.
                self._records.pop(change_set_id, None)
                raise
            hooks = scope / "hooks"
            try:
                self._creator.materialize(record, hooks=hooks)
            except AgentWorktreeError as exc:
                # Remove only artifacts whose exact generated identity exists and is
                # clean.  Any uncertain or dirty artifact remains recorded for recovery.
                record.state = "recovery"
                record.failure_class = exc.failure_class
                record.error = str(exc)
                self._save()
                if self._creator.cleanup_failed(
                    record, is_owned=self._is_owned_worktree
                ):
                    self._records.pop(change_set_id, None)
                    self._save()
                raise AgentWorktreeError(
                    exc.failure_class,
                    str(exc),
                    change_set_id=change_set_id,
                    base_sha=record.base_sha,
                    recovery_path=record.worktree_path if change_set_id in self._records else "",
                ) from exc

            record.state = "active"
            self._active_id = change_set_id
            try:
                self._save()
            except Exception as exc:
                from aura.config import redact_secrets

                # The linked worktree now exists, so keep its exact recovery
                # facts, but never strand the process-local writable slot.
                record.state = "recovery"
                record.failure_class = "worktree_record_save_failed"
                record.error = redact_secrets(f"{type(exc).__name__}: {exc}")
                try:
                    self._save()
                except Exception:
                    pass
                self._release_active()
                raise AgentWorktreeError(
                    "worktree_record_save_failed",
                    "Aura created the isolated worktree but could not persist its "
                    "active record. The worktree was preserved for recovery.",
                    change_set_id=change_set_id,
                    base_sha=record.base_sha,
                    recovery_path=record.worktree_path,
                ) from exc
            return AgentWorktree(
                change_set_id=change_set_id,
                agent_id=str(agent_id),
                base_sha=record.base_sha,
                branch_ref=record.branch_ref,
                path=Path(record.worktree_path).resolve(),
            )

    # ---- checkpoint and recovery ------------------------------------

    def checkpoint(self, worktree: AgentWorktree) -> AgentChangeSet:
        """Commit the child filesystem state as one commit directly above base."""
        with self._lock:
            record = self._matching_active_record(worktree)
            try:
                outcome = self._checkpointer.create_checkpoint(
                    record,
                    worktree.path,
                    self._scope_dir(Path(record.workspace_root)) / "hooks",
                )
                if outcome.empty:
                    record.state = "empty"
                    snapshot = self._snapshot(record, result_sha=worktree.base_sha)
                    self.cleanup(worktree, delete_branch=True)
                    self._records.pop(worktree.change_set_id, None)
                    self._save()
                    return snapshot

                record.result_sha = outcome.result_sha
                record.state = "ready"
                record.changed_paths = list(outcome.changed_paths)
                record.diffstat = outcome.diffstat
                record.failure_class = ""
                record.error = ""
                self._save()
                cleanup_error = self._cleanup_clean_worktree(record)
                if cleanup_error:
                    record.failure_class = "worktree_cleanup_failed"
                    record.error = cleanup_error
                    self._save()
                snapshot = self._snapshot(record)
                return snapshot
            except AgentWorktreeError as exc:
                record.state = "recovery"
                record.failure_class = exc.failure_class
                record.error = str(exc)
                self._save()
                raise AgentWorktreeError(
                    exc.failure_class,
                    str(exc),
                    change_set_id=worktree.change_set_id,
                    base_sha=worktree.base_sha,
                    result_sha=record.result_sha,
                    recovery_path=record.worktree_path,
                ) from exc
            finally:
                # Persistence failures are not AgentWorktreeError instances,
                # but they must release the same process-local slot too.
                if self._active_id == worktree.change_set_id:
                    self._release_active()

    def recover(self, worktree: AgentWorktree) -> AgentChangeSet:
        """Checkpoint stable partial edits after cancellation or child failure."""
        return self.checkpoint(worktree)

    # ---- observation and root decisions ------------------------------

    def list_change_sets(self) -> dict[str, Any]:
        """Return compact discoverable metadata for every unresolved record."""
        with self._lock:
            return self._inspector.list_change_sets(self._records.values())

    def inspect(
        self, change_set_id: str, *, paths: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Return compact metadata, plus a bounded diff only for exact paths."""
        with self._lock:
            record = self._require_record(change_set_id)
            return self._inspector.inspect(
                record,
                paths=paths,
                verify_result_ref=self._verify_result_ref,
                is_owned_worktree=self._is_owned_worktree,
            )

    def apply(
        self,
        change_set_id: str,
        *,
        approval_cb: ApprovalCallback,
        capture_before_write: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Approval-gate and fast-forward one result into the primary worktree."""
        with self._lock:
            record = self._require_ready_record(change_set_id)
            root = self._require_repository(change_set_id)
            # A retained linked worktree keeps its branch checked out. Remove
            # that exact clean checkout before approval; if it cannot be
            # removed, refuse before the primary checkout is touched.
            cleanup_error = self._cleanup_clean_worktree(record)
            if cleanup_error:
                raise AgentWorktreeError(
                    "worktree_cleanup_failed",
                    cleanup_error,
                    change_set_id=change_set_id,
                    base_sha=record.base_sha,
                    result_sha=record.result_sha,
                    recovery_path=record.worktree_path,
                )
            self._verify_primary_unchanged(record, root)
            self._verify_result_ref(record)
            request = self._approval_request(record)
            token = record.token()
            before_approval = self._snapshot(record).payload()

        # Approval can wait on the GUI thread. No lifecycle lock is held while
        # a person decides.
        decision = approval_cb(request)
        if decision.action in ("reject", "reject_all"):
            return {
                **before_approval,
                "ok": False,
                "applied": False,
                "failure_class": "approval_rejected",
                "error": "The Agent change set was not applied because approval was rejected.",
                "approval": decision.action,
            }

        with self._lock:
            record = self._require_ready_record(change_set_id)
            if record.token() != token:
                raise AgentWorktreeError(
                    "change_set_changed_during_approval",
                    "The retained Agent result changed while approval was pending. "
                    "Nothing was applied.",
                    change_set_id=change_set_id,
                )
            root = self._require_repository(change_set_id)
            # The approval covered these exact base/result facts.  Recheck both
            # immediately before mutation so a moved branch or new dirt cannot
            # be overwritten by a stale decision.
            self._verify_primary_unchanged(record, root)
            self._verify_result_ref(record)
            if capture_before_write is not None:
                for path in record.changed_paths:
                    capture_before_write(path)
            self._ops.fast_forward(record, root, self._scope_dir(root) / "hooks")
            snapshot = self._snapshot(record)
            response = {
                **snapshot.payload(),
                "ok": True,
                "status": "applied",
                "applied": True,
                "tool": "apply_agent_change_set",
                "approval": decision.action,
            }
            # The primary checkout is now authoritatively at result_sha. Make
            # that durable before best-effort ref/state cleanup so a crash or
            # cleanup error can never make the record claim nothing changed.
            record.state = "applied_cleanup_pending"
            record.failure_class = "change_set_cleanup_pending"
            record.error = "The change was applied; Aura still needs to clean retained Git state."
            try:
                self._save()
                self._delete_exact_ref(record, expected=record.result_sha)
                self._records.pop(change_set_id, None)
                self._save()
            except Exception as exc:
                from aura.config import redact_secrets

                warning = redact_secrets(f"{type(exc).__name__}: {exc}")
                record.state = "applied_cleanup_pending"
                record.failure_class = "change_set_cleanup_pending"
                record.error = warning
                self._records[change_set_id] = record
                try:
                    self._save()
                except Exception:
                    pass
                response["status"] = "applied_cleanup_pending"
                response["cleanup_pending"] = True
                response["warning"] = warning
            return response

    def discard(
        self,
        change_set_id: str,
        *,
        approval_cb: ApprovalCallback,
    ) -> dict[str, Any]:
        """Explicitly delete one exact Aura-owned result and no other Git state."""
        with self._lock:
            record = self._require_record(change_set_id)
            if record.state == "applied_cleanup_pending":
                return self._finish_applied_cleanup(record)
            if record.state == "discard_cleanup_pending":
                return self._finish_discard(
                    record,
                    approval="previously_approved",
                )
            # A stranded recovery checkout may contain the only copy of edits.
            # Stabilize it first so approval can describe the exact diff that
            # will be deleted, never a generic recovery path.
            if record.state != "ready" and record.worktree_path:
                if self._active_id not in ("", change_set_id):
                    raise AgentWorktreeError(
                        "writable_delegation_busy",
                        "Another writable Agent lifecycle operation is active. "
                        "The change set was preserved.",
                        change_set_id=record.change_set_id,
                        base_sha=record.base_sha,
                        result_sha=record.result_sha,
                        recovery_path=record.worktree_path,
                    )
                active = AgentWorktree(
                    record.change_set_id,
                    record.agent_id,
                    record.base_sha,
                    record.branch_ref,
                    Path(record.worktree_path),
                )
                self._active_id = record.change_set_id
                empty_or_ready = self.checkpoint(active)
                if change_set_id not in self._records:
                    return {
                        **empty_or_ready.payload(),
                        "ok": True,
                        "status": "discarded",
                        "discarded": True,
                        "tool": "discard_agent_change_set",
                        "approval": "not_required_empty",
                    }
                record = self._require_record(change_set_id)
            request = self._approval_request(record, discard=True)
            token = record.token()
            before_approval = self._snapshot(record).payload()

        decision = approval_cb(request)
        if decision.action in ("reject", "reject_all"):
            return {
                **before_approval,
                "ok": False,
                "discarded": False,
                "failure_class": "approval_rejected",
                "error": "The Agent change set was preserved because discard was rejected.",
                "approval": decision.action,
            }

        with self._lock:
            record = self._require_record(change_set_id)
            if record.token() != token:
                raise AgentWorktreeError(
                    "change_set_changed_during_approval",
                    "The retained Agent result changed while discard approval was "
                    "pending. It was preserved.",
                    change_set_id=change_set_id,
                )
            snapshot = self._snapshot(record)
            # Persist the approved discard before deleting either the exact
            # worktree registration or ref. A restart can therefore reconcile
            # any interruption without claiming the result is still retained.
            record.state = "discard_cleanup_pending"
            record.failure_class = "change_set_cleanup_pending"
            record.error = "Discard was approved; Aura still needs to clean retained Git state."
            self._save()
            return self._finish_discard(
                record,
                approval=decision.action,
                snapshot=snapshot,
            )

    def _finish_discard(
        self,
        record: _Record,
        *,
        approval: str,
        snapshot: AgentChangeSet | None = None,
    ) -> dict[str, Any]:
        """Finish an already-journaled discard and report deleted facts honestly."""
        change_set_id = record.change_set_id
        if record.worktree_path:
            if not self._is_owned_worktree(record):
                raise AgentWorktreeError(
                    "worktree_cleanup_refused",
                    "The recorded recovery worktree is not an exact Aura-owned path.",
                    change_set_id=change_set_id,
                    recovery_path=record.worktree_path,
                )
            cleanup_error = self._cleanup_clean_worktree(record)
            if cleanup_error:
                raise AgentWorktreeError(
                    "worktree_cleanup_failed",
                    cleanup_error,
                    change_set_id=change_set_id,
                    base_sha=record.base_sha,
                    result_sha=record.result_sha,
                    recovery_path=record.worktree_path,
                )
        expected = record.result_sha or record.base_sha
        self._delete_exact_ref(record, expected=expected)
        visible = snapshot or self._snapshot(record)
        self._records.pop(change_set_id, None)
        if self._active_id == change_set_id:
            self._release_active()
        try:
            self._save()
        except Exception as exc:
            from aura.config import redact_secrets

            warning = redact_secrets(f"{type(exc).__name__}: {exc}")
            record.state = "discard_cleanup_pending"
            record.failure_class = "change_set_cleanup_pending"
            record.error = warning
            self._records[change_set_id] = record
            try:
                self._save()
            except Exception:
                pass
            return {
                **visible.payload(),
                "ok": True,
                "status": "discarded_cleanup_pending",
                "discarded": True,
                "cleanup_pending": True,
                "warning": warning,
                "tool": "discard_agent_change_set",
                "approval": approval,
            }
        return {
            **visible.payload(),
            "ok": True,
            "status": "discarded",
            "discarded": True,
            "tool": "discard_agent_change_set",
            "approval": approval,
        }

    def _finish_applied_cleanup(self, record: _Record) -> dict[str, Any]:
        """Reconcile retained Git state without relabeling an applied change."""
        change_set_id = record.change_set_id
        snapshot = self._snapshot(record)
        try:
            cleanup_error = self._cleanup_clean_worktree(record)
            if cleanup_error:
                raise AgentWorktreeError(
                    "worktree_cleanup_failed",
                    cleanup_error,
                    change_set_id=change_set_id,
                    base_sha=record.base_sha,
                    result_sha=record.result_sha,
                    recovery_path=record.worktree_path,
                )
            self._delete_exact_ref(record, expected=record.result_sha)
            self._records.pop(change_set_id, None)
            self._save()
        except Exception as exc:
            from aura.config import redact_secrets

            warning = redact_secrets(f"{type(exc).__name__}: {exc}")
            record.state = "applied_cleanup_pending"
            record.failure_class = "change_set_cleanup_pending"
            record.error = warning
            self._records[change_set_id] = record
            try:
                self._save()
            except Exception:
                pass
            return {
                **snapshot.payload(),
                "ok": True,
                "status": "applied_cleanup_pending",
                "applied": True,
                "discarded": False,
                "cleanup_pending": True,
                "warning": warning,
                "tool": "discard_agent_change_set",
                "approval": "not_required_already_applied",
            }
        return {
            **snapshot.payload(),
            "ok": True,
            "status": "applied",
            "applied": True,
            "discarded": False,
            "tool": "discard_agent_change_set",
            "approval": "not_required_already_applied",
        }

    def cleanup(self, worktree: AgentWorktree, *, delete_branch: bool = False) -> None:
        """Remove one exact clean Aura-owned linked worktree."""
        with self._lock:
            record = self._records.get(worktree.change_set_id)
            if record is None or not self._record_matches(worktree, record):
                raise AgentWorktreeError(
                    "worktree_cleanup_refused",
                    "The requested worktree is not the recorded Aura-owned worktree.",
                    change_set_id=worktree.change_set_id,
                )
            error = self._cleanup_clean_worktree(record)
            if error:
                raise AgentWorktreeError(
                    "worktree_cleanup_failed",
                    error,
                    change_set_id=worktree.change_set_id,
                    base_sha=worktree.base_sha,
                    result_sha=record.result_sha,
                    recovery_path=record.worktree_path,
                )
            if delete_branch:
                self._delete_exact_ref(record, expected=record.result_sha or record.base_sha)

    # ---- approval material -------------------------------------------

    def _approval_request(self, record: _Record, *, discard: bool = False) -> ApprovalRequest:
        return self._material.approval_request(record, discard=discard)

    # ---- invariants and Git helpers ----------------------------------

    def _require_repository(self, change_set_id: str) -> Path:
        return self._ops.require_repository(self._workspace_root, change_set_id)

    def _verify_primary_unchanged(self, record: _Record, root: Path) -> None:
        self._ops.verify_primary_unchanged(record, root)

    def _verify_result_ref(self, record: _Record) -> None:
        self._ops.verify_result_ref(record)

    def _delete_exact_ref(self, record: _Record, *, expected: str) -> None:
        self._ops.delete_exact_ref(record, expected=expected)

    def _cleanup_clean_worktree(self, record: _Record) -> str:
        before = record.worktree_path
        error = self._ops.cleanup_clean_worktree(
            record, is_owned=self._is_owned_worktree
        )
        if record.worktree_path != before:
            self._save()
        return error

    def _matching_active_record(self, worktree: AgentWorktree) -> _Record:
        record = self._records.get(worktree.change_set_id)
        if record is None or not self._record_matches(worktree, record):
            raise AgentWorktreeError(
                "worktree_not_owned",
                "The writable checkout is not the active Aura-owned worktree.",
                change_set_id=worktree.change_set_id,
            )
        if self._active_id not in ("", worktree.change_set_id):
            raise AgentWorktreeError(
                "writable_delegation_busy",
                "Another writable Agent lifecycle operation is active.",
                change_set_id=worktree.change_set_id,
            )
        self._active_id = worktree.change_set_id
        return record

    def _release_active(self) -> None:
        self._active_id = ""
        if not self._pending_workspace_change:
            return
        pending = self._pending_workspace_root
        self._pending_workspace_root = None
        self._pending_workspace_change = False
        self._rebind(pending)

    def _rebind(self, root: Path | str | None) -> None:
        self._workspace_root = Path(root).resolve() if root is not None else None
        self._records = {}
        self._reload()

    @staticmethod
    def _record_matches(worktree: AgentWorktree, record: _Record) -> bool:
        return (
            record.base_sha == worktree.base_sha
            and record.branch_ref == worktree.branch_ref
            and Path(record.worktree_path).resolve() == worktree.path.resolve()
        )

    def _require_record(self, change_set_id: str) -> _Record:
        record = self._records.get(str(change_set_id or "").strip())
        if record is None:
            raise AgentWorktreeError(
                "change_set_not_found",
                f"No unresolved Agent change set has id '{change_set_id}'.",
                change_set_id=str(change_set_id or ""),
            )
        if Path(record.workspace_root).resolve() != (self._workspace_root or Path()).resolve():
            raise AgentWorktreeError(
                "change_set_workspace_mismatch",
                "That Agent change set belongs to a different workspace.",
                change_set_id=record.change_set_id,
            )
        return record

    def _require_ready_record(self, change_set_id: str) -> _Record:
        record = self._require_record(change_set_id)
        if record.state != "ready" or not record.result_sha:
            raise AgentWorktreeError(
                "change_set_not_ready",
                "That Agent change set has no stable checkpoint to apply. Its recovery "
                "worktree was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                recovery_path=record.worktree_path,
            )
        return record

    def _is_owned_worktree(self, record: _Record) -> bool:
        root = self._scope_dir(Path(record.workspace_root)).resolve()
        path = Path(record.worktree_path).resolve()
        return (
            path.name == record.change_set_id
            and safe_is_relative_to(path, root)
            and path.parent == root
        )

    def _snapshot(self, record: _Record, *, result_sha: str | None = None) -> AgentChangeSet:
        return AgentChangeSet(
            status=record.state,
            change_set_id=record.change_set_id,
            agent_id=record.agent_id,
            base_sha=record.base_sha,
            result_sha=record.result_sha if result_sha is None else result_sha,
            changed_paths=tuple(record.changed_paths),
            diffstat=record.diffstat,
            worktree_path=record.worktree_path,
            failure_class=record.failure_class,
            error=record.error,
        )

    # ---- minimal persistence ----------------------------------------

    def _scope_dir(self, root: Path) -> Path:
        return self._record_store.scope_dir(root)

    def _reload(self) -> None:
        self._record_store.bind(self._workspace_root)
        self._records = {
            change_set_id: record
            for change_set_id, record in self._record_store.records.items()
            if record.branch_ref.startswith(_BRANCH_PREFIX)
        }
        self._record_store.records = self._records

    def _save(self) -> None:
        self._record_store.records = self._records
        self._record_store.save()


__all__ = [
    "AgentChangeSet",
    "AgentWorktree",
    "AgentWorktreeError",
    "AgentWorktreeManager",
]
