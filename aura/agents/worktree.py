"""Git worktree lifecycle for writable delegated agents.

``AgentWorktreeManager`` is the only owner of writable child state.  A run is
created from a clean primary worktree at an exact commit, checkpointed as one
runtime commit, inspected without touching the primary checkout, and either
fast-forwarded into that checkout or explicitly discarded.

The linked worktree is isolation for Git changes, not an operating-system
sandbox.  In particular, a terminal-enabled child still runs with the user's
authority and can reach absolute paths, the network, credentials, and Git's
shared metadata.
"""
from __future__ import annotations

import json
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalFileChange,
    ApprovalRequest,
)
from aura.conversation.tools.fs_write import atomic_write_bytes
from aura.paths import data_dir, safe_is_relative_to

_BRANCH_PREFIX = "refs/heads/aura/agent/"
_GIT_IDENTITY = (
    "-c", "user.name=Aura Agent",
    "-c", "user.email=agent@aura.local",
    "-c", "commit.gpgSign=false",
)


class AgentWorktreeError(RuntimeError):
    """A focused lifecycle failure that is safe to report to the root."""

    def __init__(
        self,
        failure_class: str,
        message: str,
        *,
        change_set_id: str = "",
        base_sha: str = "",
        result_sha: str = "",
        recovery_path: str = "",
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.change_set_id = change_set_id
        self.base_sha = base_sha
        self.result_sha = result_sha
        self.recovery_path = recovery_path

    def payload(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "error": str(self),
            "change_set_id": self.change_set_id,
            "base_sha": self.base_sha,
            "result_sha": self.result_sha,
            "recovery_path": self.recovery_path,
        }


@dataclass(frozen=True)
class AgentWorktree:
    """One active writable child checkout."""

    change_set_id: str
    agent_id: str
    base_sha: str
    branch_ref: str
    path: Path


@dataclass(frozen=True)
class AgentChangeSet:
    """The minimal durable facts about a checkpointed child result."""

    status: str
    change_set_id: str
    agent_id: str
    base_sha: str
    result_sha: str = ""
    changed_paths: tuple[str, ...] = ()
    diffstat: str = ""
    worktree_path: str = ""
    failure_class: str = ""
    error: str = ""

    @property
    def retained(self) -> bool:
        return self.status != "empty" and bool(self.result_sha or self.worktree_path)

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "change_set_id": self.change_set_id,
            "agent_id": self.agent_id,
            "base_sha": self.base_sha,
            "result_sha": self.result_sha,
            "changed_paths": list(self.changed_paths),
            "diffstat": self.diffstat,
            "worktree_path": self.worktree_path,
            "failure_class": self.failure_class,
            "error": self.error,
        }


@dataclass
class _Record:
    change_set_id: str
    agent_id: str
    workspace_root: str
    base_sha: str
    primary_ref: str
    branch_ref: str
    worktree_path: str
    result_sha: str = ""
    state: str = "creating"
    changed_paths: list[str] = field(default_factory=list)
    diffstat: str = ""
    failure_class: str = ""
    error: str = ""

    @classmethod
    def from_json(cls, raw: object) -> "_Record | None":
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                change_set_id=str(raw["change_set_id"]),
                agent_id=str(raw["agent_id"]),
                workspace_root=str(raw["workspace_root"]),
                base_sha=str(raw["base_sha"]),
                primary_ref=str(raw.get("primary_ref") or ""),
                branch_ref=str(raw["branch_ref"]),
                worktree_path=str(raw["worktree_path"]),
                result_sha=str(raw.get("result_sha") or ""),
                state=str(raw.get("state") or "recovery"),
                changed_paths=[str(p) for p in raw.get("changed_paths", [])],
                diffstat=str(raw.get("diffstat") or ""),
                failure_class=str(raw.get("failure_class") or ""),
                error=str(raw.get("error") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


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
            base_sha = self._rev_parse(root, "HEAD", change_set_id=change_set_id)
            primary_ref = self._symbolic_head(root, change_set_id=change_set_id)
            dirty = self._status_bytes(root, change_set_id=change_set_id)
            if dirty:
                raise AgentWorktreeError(
                    "primary_worktree_dirty",
                    "Writable delegation requires a completely clean primary worktree; "
                    "staged, unstaged, or untracked changes are present.",
                    change_set_id=change_set_id,
                    base_sha=base_sha,
                )

            scope = self._scope_dir(root)
            worktree_path = scope / change_set_id
            branch_ref = f"{_BRANCH_PREFIX}{self._workspace_token(root)}/{change_set_id}"
            branch_name = branch_ref.removeprefix("refs/heads/")
            if worktree_path.exists() or self._ref_sha(root, branch_ref):
                raise AgentWorktreeError(
                    "worktree_name_collision",
                    "Aura generated a worktree identity that is already in use.",
                    change_set_id=change_set_id,
                    base_sha=base_sha,
                )

            record = _Record(
                change_set_id=change_set_id,
                agent_id=str(agent_id),
                workspace_root=str(root),
                base_sha=base_sha,
                primary_ref=primary_ref,
                branch_ref=branch_ref,
                worktree_path=str(worktree_path),
            )
            self._records[change_set_id] = record
            self._save()
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            hooks = scope / "hooks"
            hooks.mkdir(parents=True, exist_ok=True)
            try:
                self._git(
                    root,
                    [
                        "-c",
                        f"core.hooksPath={hooks}",
                        "worktree",
                        "add",
                        "-b",
                        branch_name,
                        str(worktree_path),
                        base_sha,
                    ],
                    change_set_id=change_set_id,
                    failure_class="worktree_creation_failed",
                )
            except AgentWorktreeError as exc:
                # Remove only artifacts whose exact generated identity exists and is
                # clean.  Any uncertain or dirty artifact remains recorded for recovery.
                record.state = "recovery"
                record.failure_class = exc.failure_class
                record.error = str(exc)
                self._save()
                self._cleanup_failed_creation(record)
                raise AgentWorktreeError(
                    exc.failure_class,
                    str(exc),
                    change_set_id=change_set_id,
                    base_sha=base_sha,
                    recovery_path=record.worktree_path if change_set_id in self._records else "",
                ) from exc

            record.state = "active"
            self._active_id = change_set_id
            self._save()
            return AgentWorktree(
                change_set_id=change_set_id,
                agent_id=str(agent_id),
                base_sha=base_sha,
                branch_ref=branch_ref,
                path=worktree_path.resolve(),
            )

    # ---- checkpoint and recovery ------------------------------------

    def checkpoint(self, worktree: AgentWorktree) -> AgentChangeSet:
        """Commit the child filesystem state as one commit directly above base."""
        with self._lock:
            record = self._matching_active_record(worktree)
            try:
                symbolic = self._git_text(
                    worktree.path,
                    ["symbolic-ref", "-q", "HEAD"],
                    change_set_id=worktree.change_set_id,
                    failure_class="checkpoint_failed",
                ).strip()
                if symbolic != worktree.branch_ref:
                    raise AgentWorktreeError(
                        "checkpoint_ref_changed",
                        "The child left its Aura-owned branch. Its worktree was preserved "
                        "for recovery and no foreign ref was changed.",
                        change_set_id=worktree.change_set_id,
                        base_sha=worktree.base_sha,
                        recovery_path=str(worktree.path),
                    )

                # A terminal-enabled child may have made intermediate commits.  Soft
                # reset only the exact Aura-owned checked-out ref, then stage the final
                # filesystem state so the retained result is still one runtime commit.
                current = self._rev_parse(
                    worktree.path, "HEAD", change_set_id=worktree.change_set_id
                )
                if current != worktree.base_sha:
                    self._git(
                        worktree.path,
                        ["reset", "--soft", worktree.base_sha],
                        change_set_id=worktree.change_set_id,
                        failure_class="checkpoint_failed",
                    )
                self._git(
                    worktree.path,
                    ["add", "-A", "--", "."],
                    change_set_id=worktree.change_set_id,
                    failure_class="checkpoint_failed",
                )
                staged = self._git(
                    worktree.path,
                    ["diff", "--cached", "--quiet", "--exit-code"],
                    check=False,
                    change_set_id=worktree.change_set_id,
                    failure_class="checkpoint_failed",
                )
                if staged.returncode not in (0, 1):
                    self._raise_git_failure(
                        staged,
                        "checkpoint_failed",
                        worktree.change_set_id,
                        worktree.base_sha,
                        str(worktree.path),
                    )
                if staged.returncode == 0:
                    record.state = "empty"
                    snapshot = self._snapshot(record, result_sha=worktree.base_sha)
                    self.cleanup(worktree, delete_branch=True)
                    self._records.pop(worktree.change_set_id, None)
                    self._save()
                    self._release_active()
                    return snapshot

                hooks = self._scope_dir(Path(record.workspace_root)) / "hooks"
                hooks.mkdir(parents=True, exist_ok=True)
                message = f"Aura agent change set {worktree.change_set_id}"
                self._git(
                    worktree.path,
                    [*_GIT_IDENTITY, "-c", f"core.hooksPath={hooks}", "commit", "--no-verify", "-m", message],
                    change_set_id=worktree.change_set_id,
                    failure_class="checkpoint_failed",
                )
                result_sha = self._rev_parse(
                    worktree.path, "HEAD", change_set_id=worktree.change_set_id
                )
                parents = self._git_text(
                    worktree.path,
                    ["rev-list", "--parents", "-n", "1", result_sha],
                    change_set_id=worktree.change_set_id,
                    failure_class="checkpoint_failed",
                ).strip().split()
                if len(parents) != 2 or parents[1] != worktree.base_sha:
                    raise AgentWorktreeError(
                        "checkpoint_shape_invalid",
                        "The Agent result was not one commit directly above its frozen base.",
                        change_set_id=worktree.change_set_id,
                        base_sha=worktree.base_sha,
                        result_sha=result_sha,
                        recovery_path=str(worktree.path),
                    )

                changed_paths = self._changed_paths(
                    worktree.path, worktree.base_sha, result_sha, worktree.change_set_id
                )
                diffstat = self._diffstat(
                    worktree.path, worktree.base_sha, result_sha, worktree.change_set_id
                )
                record.result_sha = result_sha
                record.state = "ready"
                record.changed_paths = list(changed_paths)
                record.diffstat = diffstat
                record.failure_class = ""
                record.error = ""
                self._save()
                cleanup_error = self._cleanup_clean_worktree(record)
                if cleanup_error:
                    record.failure_class = "worktree_cleanup_failed"
                    record.error = cleanup_error
                    self._save()
                snapshot = self._snapshot(record)
                self._release_active()
                return snapshot
            except AgentWorktreeError as exc:
                record.state = "recovery"
                record.failure_class = exc.failure_class
                record.error = str(exc)
                self._save()
                self._release_active()
                raise AgentWorktreeError(
                    exc.failure_class,
                    str(exc),
                    change_set_id=worktree.change_set_id,
                    base_sha=worktree.base_sha,
                    result_sha=record.result_sha,
                    recovery_path=record.worktree_path,
                ) from exc

    def recover(self, worktree: AgentWorktree) -> AgentChangeSet:
        """Checkpoint stable partial edits after cancellation or child failure."""
        return self.checkpoint(worktree)

    # ---- observation and root decisions ------------------------------

    def inspect(self, change_set_id: str) -> dict[str, Any]:
        """Return an observational diff for one retained result."""
        with self._lock:
            record = self._require_record(change_set_id)
            payload = self._snapshot(record).payload()
            if record.result_sha:
                self._verify_result_ref(record)
                payload["diff"] = self._git_text(
                    Path(record.workspace_root),
                    ["diff", "--find-renames", "--no-ext-diff", record.base_sha, record.result_sha, "--"],
                    change_set_id=change_set_id,
                    failure_class="change_set_inspection_failed",
                )
            else:
                recovery = Path(record.worktree_path)
                payload["diff"] = ""
                if recovery.is_dir() and self._is_owned_worktree(record):
                    payload["worktree_status"] = self._git_text(
                        recovery,
                        ["status", "--short", "--untracked-files=all"],
                        check=False,
                        change_set_id=change_set_id,
                        failure_class="change_set_inspection_failed",
                    )
            payload["ok"] = True
            payload["tool"] = "inspect_agent_change_set"
            return payload

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
            self._verify_primary_unchanged(record, root)
            self._verify_result_ref(record)
            request = self._approval_request(record)
            decision = approval_cb(request)
            if decision.action in ("reject", "reject_all"):
                return {
                    **self._snapshot(record).payload(),
                    "ok": False,
                    "applied": False,
                    "failure_class": "approval_rejected",
                    "error": "The Agent change set was not applied because approval was rejected.",
                    "approval": decision.action,
                }

            # The approval covered these exact base/result facts.  Recheck both
            # immediately before mutation so a moved branch or new dirt cannot
            # be overwritten by a stale decision.
            self._verify_primary_unchanged(record, root)
            self._verify_result_ref(record)
            if capture_before_write is not None:
                for path in record.changed_paths:
                    capture_before_write(path)
            self._git(
                root,
                [
                    "-c",
                    f"core.hooksPath={self._scope_dir(root) / 'hooks'}",
                    "merge",
                    "--ff-only",
                    "--no-edit",
                    record.result_sha,
                ],
                change_set_id=change_set_id,
                failure_class="change_set_apply_failed",
            )
            landed = self._rev_parse(root, "HEAD", change_set_id=change_set_id)
            if landed != record.result_sha:
                raise AgentWorktreeError(
                    "change_set_apply_failed",
                    "Git did not leave the primary worktree at the approved result commit.",
                    change_set_id=change_set_id,
                    base_sha=record.base_sha,
                    result_sha=record.result_sha,
                )
            self._delete_exact_ref(record, expected=record.result_sha)
            snapshot = self._snapshot(record)
            self._records.pop(change_set_id, None)
            self._save()
            return {
                **snapshot.payload(),
                "ok": True,
                "status": "applied",
                "applied": True,
                "tool": "apply_agent_change_set",
                "approval": decision.action,
            }

    def discard(
        self,
        change_set_id: str,
        *,
        approval_cb: ApprovalCallback,
    ) -> dict[str, Any]:
        """Explicitly delete one exact Aura-owned result and no other Git state."""
        with self._lock:
            record = self._require_record(change_set_id)
            request = self._approval_request(record, discard=True)
            decision = approval_cb(request)
            if decision.action in ("reject", "reject_all"):
                return {
                    **self._snapshot(record).payload(),
                    "ok": False,
                    "discarded": False,
                    "failure_class": "approval_rejected",
                    "error": "The Agent change set was preserved because discard was rejected.",
                    "approval": decision.action,
                }

            if record.worktree_path and Path(record.worktree_path).exists():
                if not self._is_owned_worktree(record):
                    raise AgentWorktreeError(
                        "worktree_cleanup_refused",
                        "The recorded recovery worktree is not an exact Aura-owned path.",
                        change_set_id=change_set_id,
                        recovery_path=record.worktree_path,
                    )
                # Never force-remove recoverable dirt.  Make one last stable
                # checkpoint first; if that fails the worktree remains intact.
                if record.state != "ready":
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
                            "approval": decision.action,
                        }
                    record = self._require_record(change_set_id)
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
            snapshot = self._snapshot(record)
            self._records.pop(change_set_id, None)
            if self._active_id == change_set_id:
                self._release_active()
            self._save()
            return {
                **snapshot.payload(),
                "ok": True,
                "status": "discarded",
                "discarded": True,
                "tool": "discard_agent_change_set",
                "approval": decision.action,
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
        changes = self._approval_changes(record)
        if changes:
            first = changes[0]
        else:
            first = ApprovalFileChange(
                rel_path=f"Agent change set {record.change_set_id}",
                old_content="",
                new_content=record.diffstat,
                is_new_file=False,
            )
            changes = (first,)
        return ApprovalRequest(
            tool_name=("discard_agent_change_set" if discard else "apply_agent_change_set"),
            rel_path=first.rel_path,
            old_content=first.old_content,
            new_content=first.new_content,
            is_new_file=first.is_new_file,
            changes=changes,
        )

    def _approval_changes(self, record: _Record) -> tuple[ApprovalFileChange, ...]:
        if not record.result_sha:
            return ()
        rows = self._name_status(record)
        changes: list[ApprovalFileChange] = []
        for status, old_path, new_path in rows:
            if status == "A":
                old = ""
                new = self._commit_content(record, record.result_sha, new_path)
                rel_path = new_path
                is_new = True
            elif status == "D":
                old = self._commit_content(record, record.base_sha, old_path)
                new = ""
                rel_path = old_path
                is_new = False
            elif status == "R":
                old = self._commit_content(record, record.base_sha, old_path)
                new = self._commit_content(record, record.result_sha, new_path)
                changes.append(ApprovalFileChange(old_path, old, "", False, "delete"))
                changes.append(ApprovalFileChange(new_path, "", new, True, "create"))
                continue
            elif status == "C":
                new = self._commit_content(record, record.result_sha, new_path)
                changes.append(ApprovalFileChange(new_path, "", new, True, "create"))
                continue
            else:
                old = self._commit_content(record, record.base_sha, old_path)
                new = self._commit_content(record, record.result_sha, new_path)
                rel_path = (
                    f"{old_path} -> {new_path}" if old_path != new_path else new_path
                )
                is_new = False
            action = "create" if status == "A" else "delete" if status == "D" else "modify"
            changes.append(ApprovalFileChange(rel_path, old, new, is_new, action))
        return tuple(changes)

    def _commit_content(self, record: _Record, sha: str, path: str) -> str:
        proc = self._git(
            Path(record.workspace_root),
            ["show", f"{sha}:{path}"],
            text=False,
            check=False,
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        )
        if proc.returncode != 0:
            return ""
        data = bytes(proc.stdout or b"")
        try:
            if b"\x00" in data:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "binary")
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return f"[binary content: {len(data)} bytes]"

    # ---- invariants and Git helpers ----------------------------------

    def _require_repository(self, change_set_id: str) -> Path:
        root = self._workspace_root
        if root is None or not root.is_dir():
            raise AgentWorktreeError(
                "git_repository_required",
                "Writable delegation requires an open Git repository.",
                change_set_id=change_set_id,
            )
        proc = self._git(
            root,
            ["rev-parse", "--show-toplevel"],
            check=False,
            change_set_id=change_set_id,
            failure_class="git_repository_required",
        )
        if proc.returncode != 0:
            raise AgentWorktreeError(
                "git_repository_required",
                "Writable delegation requires a Git repository.",
                change_set_id=change_set_id,
            )
        top = Path(str(proc.stdout).strip()).resolve()
        if top != root.resolve():
            raise AgentWorktreeError(
                "git_repository_root_required",
                "Writable delegation requires the open workspace to be the Git repository root.",
                change_set_id=change_set_id,
            )
        bare = self._git_text(
            root,
            ["rev-parse", "--is-bare-repository"],
            change_set_id=change_set_id,
            failure_class="git_repository_required",
        ).strip()
        if bare != "false":
            raise AgentWorktreeError(
                "git_repository_required",
                "Writable delegation requires a non-bare Git working repository.",
                change_set_id=change_set_id,
            )
        return root

    def _verify_primary_unchanged(self, record: _Record, root: Path) -> None:
        current_ref = self._symbolic_head(root, change_set_id=record.change_set_id)
        if current_ref != record.primary_ref:
            raise AgentWorktreeError(
                "primary_branch_moved",
                "The primary checkout changed branches after this Agent started. Nothing "
                "was applied; the Agent result was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )
        head = self._rev_parse(root, "HEAD", change_set_id=record.change_set_id)
        if head != record.base_sha:
            raise AgentWorktreeError(
                "primary_branch_moved",
                "The primary branch moved after this Agent started. Nothing was applied; "
                "the Agent result was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )
        if self._status_bytes(root, change_set_id=record.change_set_id):
            raise AgentWorktreeError(
                "primary_worktree_dirty",
                "The primary worktree is no longer clean. Nothing was applied; the Agent "
                "result was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )

    def _verify_result_ref(self, record: _Record) -> None:
        if not record.branch_ref.startswith(_BRANCH_PREFIX):
            raise AgentWorktreeError(
                "change_set_ref_invalid",
                "The recorded result ref is not Aura-owned.",
                change_set_id=record.change_set_id,
            )
        current = self._ref_sha(Path(record.workspace_root), record.branch_ref)
        if not current or current != record.result_sha:
            raise AgentWorktreeError(
                "change_set_ref_changed",
                "The Aura-owned result ref moved or disappeared. Nothing was changed.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )

    def _delete_exact_ref(self, record: _Record, *, expected: str) -> None:
        if not record.branch_ref.startswith(_BRANCH_PREFIX):
            raise AgentWorktreeError(
                "change_set_ref_invalid",
                "Refusing to delete a ref that is not Aura-owned.",
                change_set_id=record.change_set_id,
            )
        current = self._ref_sha(Path(record.workspace_root), record.branch_ref)
        if not current:
            return
        if current != expected:
            raise AgentWorktreeError(
                "change_set_ref_changed",
                "The Aura-owned ref no longer names the expected commit and was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )
        self._git(
            Path(record.workspace_root),
            ["update-ref", "-d", record.branch_ref, expected],
            change_set_id=record.change_set_id,
            failure_class="change_set_cleanup_failed",
        )

    def _cleanup_clean_worktree(self, record: _Record) -> str:
        path = Path(record.worktree_path)
        if not path.exists():
            record.worktree_path = ""
            self._save()
            return ""
        if not self._is_owned_worktree(record):
            return "Refusing to remove a path that is not the exact Aura-owned worktree."
        try:
            if self._status_bytes(path, change_set_id=record.change_set_id):
                return "The recovery worktree is dirty and was preserved."
            self._git(
                Path(record.workspace_root),
                ["worktree", "remove", str(path)],
                change_set_id=record.change_set_id,
                failure_class="worktree_cleanup_failed",
            )
        except AgentWorktreeError as exc:
            return str(exc)
        record.worktree_path = ""
        self._save()
        return ""

    def _cleanup_failed_creation(self, record: _Record) -> None:
        path = Path(record.worktree_path)
        if path.exists() and self._is_owned_worktree(record):
            try:
                path.rmdir()
            except OSError:
                pass
        if path.exists() and self._is_owned_worktree(record):
            try:
                if not self._status_bytes(path, change_set_id=record.change_set_id):
                    self._git(
                        Path(record.workspace_root),
                        ["worktree", "remove", str(path)],
                        check=False,
                        change_set_id=record.change_set_id,
                        failure_class="worktree_cleanup_failed",
                    )
            except AgentWorktreeError:
                return
        if path.exists():
            return
        current = self._ref_sha(Path(record.workspace_root), record.branch_ref)
        if current == record.base_sha:
            try:
                self._delete_exact_ref(record, expected=record.base_sha)
            except AgentWorktreeError:
                return
        if not self._ref_sha(Path(record.workspace_root), record.branch_ref):
            self._records.pop(record.change_set_id, None)
            self._save()

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

    def _name_status(self, record: _Record) -> list[tuple[str, str, str]]:
        proc = self._git(
            Path(record.workspace_root),
            ["diff", "--name-status", "-z", "-M", record.base_sha, record.result_sha, "--"],
            text=False,
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        )
        tokens = [
            item.decode("utf-8", "surrogateescape")
            for item in bytes(proc.stdout or b"").split(b"\0")
            if item
        ]
        rows: list[tuple[str, str, str]] = []
        index = 0
        while index < len(tokens):
            status_token = tokens[index]
            index += 1
            kind = status_token[:1]
            if kind in ("R", "C") and index + 1 < len(tokens):
                old_path, new_path = tokens[index], tokens[index + 1]
                index += 2
            elif index < len(tokens):
                old_path = new_path = tokens[index]
                index += 1
            else:
                break
            rows.append((kind, old_path, new_path))
        return rows

    def _changed_paths(self, root: Path, base: str, result: str, change_set_id: str) -> tuple[str, ...]:
        record = _Record(
            change_set_id,
            "",
            str(root),
            base,
            "",
            "",
            "",
            result_sha=result,
        )
        paths: list[str] = []
        for _status, old_path, new_path in self._name_status(record):
            for path in (old_path, new_path):
                if path and path not in paths:
                    paths.append(path)
        return tuple(paths)

    def _diffstat(self, root: Path, base: str, result: str, change_set_id: str) -> str:
        return self._git_text(
            root,
            ["diff", "--stat", "--summary", "--find-renames", base, result, "--"],
            change_set_id=change_set_id,
            failure_class="checkpoint_failed",
        ).strip()

    def _status_bytes(self, root: Path, *, change_set_id: str) -> bytes:
        proc = self._git(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            text=False,
            change_set_id=change_set_id,
            failure_class="git_status_failed",
        )
        return bytes(proc.stdout or b"")

    def _rev_parse(self, root: Path, rev: str, *, change_set_id: str) -> str:
        return self._git_text(
            root,
            ["rev-parse", "--verify", rev],
            change_set_id=change_set_id,
            failure_class="git_revision_failed",
        ).strip()

    def _symbolic_head(self, root: Path, *, change_set_id: str) -> str:
        proc = self._git(
            root,
            ["symbolic-ref", "-q", "HEAD"],
            check=False,
            change_set_id=change_set_id,
            failure_class="git_revision_failed",
        )
        if proc.returncode not in (0, 1):
            self._raise_git_failure(proc, "git_revision_failed", change_set_id)
        return str(proc.stdout or "").strip() if proc.returncode == 0 else ""

    def _ref_sha(self, root: Path, ref: str) -> str:
        proc = self._git(
            root,
            ["rev-parse", "--verify", "--quiet", ref],
            check=False,
            change_set_id="",
            failure_class="git_revision_failed",
        )
        return str(proc.stdout).strip() if proc.returncode == 0 else ""

    def _git_text(
        self,
        root: Path,
        args: list[str],
        *,
        check: bool = True,
        change_set_id: str,
        failure_class: str,
    ) -> str:
        proc = self._git(
            root,
            args,
            text=True,
            check=check,
            change_set_id=change_set_id,
            failure_class=failure_class,
        )
        return str(proc.stdout or "")

    def _git(
        self,
        root: Path,
        args: list[str],
        *,
        text: bool = True,
        check: bool = True,
        change_set_id: str,
        failure_class: str,
    ) -> subprocess.CompletedProcess[Any]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text,
                encoding="utf-8" if text else None,
                errors="replace" if text else None,
                check=False,
                shell=False,
            )
        except OSError as exc:
            raise AgentWorktreeError(
                failure_class,
                f"Git could not be started: {exc}",
                change_set_id=change_set_id,
            ) from exc
        if check and proc.returncode != 0:
            self._raise_git_failure(proc, failure_class, change_set_id)
        return proc

    @staticmethod
    def _raise_git_failure(
        proc: subprocess.CompletedProcess[Any],
        failure_class: str,
        change_set_id: str,
        base_sha: str = "",
        recovery_path: str = "",
    ) -> None:
        stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
        stdout = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
        detail = (stderr or stdout or f"Git exited with code {proc.returncode}").strip()
        raise AgentWorktreeError(
            failure_class,
            detail,
            change_set_id=change_set_id,
            base_sha=base_sha,
            recovery_path=recovery_path,
        )

    # ---- minimal persistence ----------------------------------------

    def _workspace_token(self, root: Path) -> str:
        from aura.agents.local_state import workspace_key

        return workspace_key(root)[:12]

    def _scope_dir(self, root: Path) -> Path:
        return self._runtime_base / self._workspace_token(root)

    def _state_path(self) -> Path | None:
        root = self._workspace_root
        return (self._scope_dir(root) / "state.json") if root is not None else None

    def _reload(self) -> None:
        path = self._state_path()
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        records = raw.get("change_sets") if isinstance(raw, dict) else None
        if not isinstance(records, list):
            return
        expected_root = str(self._workspace_root)
        for item in records:
            record = _Record.from_json(item)
            if (
                record is not None
                and record.workspace_root == expected_root
                and record.branch_ref.startswith(_BRANCH_PREFIX)
            ):
                self._records[record.change_set_id] = record

    def _save(self) -> None:
        path = self._state_path()
        if path is None:
            return
        payload = {
            "version": 1,
            "workspace": str(self._workspace_root),
            "change_sets": [asdict(record) for record in self._records.values()],
        }
        atomic_write_bytes(
            path,
            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        )


__all__ = [
    "AgentChangeSet",
    "AgentWorktree",
    "AgentWorktreeError",
    "AgentWorktreeManager",
]
