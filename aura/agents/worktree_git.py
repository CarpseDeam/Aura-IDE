"""Running Git for the writable-Agent lifecycle, and naming what went wrong.

This module owns one job: invoke Git safely and turn any failure into an
:class:`AgentWorktreeError` the root can be told about. It holds no lifecycle
state, decides no policy, and knows nothing about change sets beyond the id it
is handed to label a failure with.

:class:`~aura.agents.worktree.AgentWorktreeManager` owns the policy above it —
when a worktree may be created, what a checkpoint must look like, when a result
may be applied — and reaches through :class:`GitRunner` for every Git call it
makes, so the mechanics of running a subprocess and the rules of the lifecycle
never live in the same place.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from aura.config import get_subprocess_kwargs


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


class GitRunner:
    """Runs Git in a given directory and reports failures as lifecycle errors.

    Every method takes the ``change_set_id`` and ``failure_class`` the caller
    wants a failure labelled with, because only the caller knows which step of
    the lifecycle it is in. Nothing here retries, falls back, or interprets a
    non-zero exit as anything other than what the caller said it means.
    """

    def run(
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
                # Every other Git call site in Aura runs through this, and a
                # writable delegation makes a dozen of them per run: without it
                # each one flashes a console window over the app on Windows.
                **get_subprocess_kwargs(),
            )
        except OSError as exc:
            raise AgentWorktreeError(
                failure_class,
                f"Git could not be started: {exc}",
                change_set_id=change_set_id,
            ) from exc
        if check and proc.returncode != 0:
            self.raise_failure(proc, failure_class, change_set_id)
        return proc

    def text(
        self,
        root: Path,
        args: list[str],
        *,
        check: bool = True,
        change_set_id: str,
        failure_class: str,
    ) -> str:
        proc = self.run(
            root,
            args,
            text=True,
            check=check,
            change_set_id=change_set_id,
            failure_class=failure_class,
        )
        return str(proc.stdout or "")

    def status_bytes(self, root: Path, *, change_set_id: str) -> bytes:
        """Everything Git considers uncommitted, including untracked files."""
        proc = self.run(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            text=False,
            change_set_id=change_set_id,
            failure_class="git_status_failed",
        )
        return bytes(proc.stdout or b"")

    def rev_parse(self, root: Path, rev: str, *, change_set_id: str) -> str:
        return self.text(
            root,
            ["rev-parse", "--verify", rev],
            change_set_id=change_set_id,
            failure_class="git_revision_failed",
        ).strip()

    def symbolic_head(self, root: Path, *, change_set_id: str) -> str:
        """The checked-out ref, or ``""`` on a detached HEAD."""
        proc = self.run(
            root,
            ["symbolic-ref", "-q", "HEAD"],
            check=False,
            change_set_id=change_set_id,
            failure_class="git_revision_failed",
        )
        if proc.returncode not in (0, 1):
            self.raise_failure(proc, "git_revision_failed", change_set_id)
        return str(proc.stdout or "").strip() if proc.returncode == 0 else ""

    def ref_sha(
        self, root: Path, ref: str, *, change_set_id: str = ""
    ) -> str | None:
        """The commit *ref* names, or ``None`` only when it is missing.

        A permission, repository, or process failure is not a missing ref and
        is raised honestly instead of being collapsed into the same empty value.
        """
        proc = self.run(
            root,
            ["rev-parse", "--verify", "--quiet", ref],
            check=False,
            change_set_id=change_set_id,
            failure_class="git_revision_failed",
        )
        if proc.returncode == 0:
            return str(proc.stdout).strip()
        if proc.returncode == 1:
            return None
        self.raise_failure(proc, "git_revision_failed", change_set_id)
        raise AssertionError("unreachable")

    def bounded_bytes(
        self,
        root: Path,
        args: list[str],
        *,
        max_bytes: int,
        change_set_id: str,
        failure_class: str,
    ) -> tuple[bytes, int, bool, str]:
        """Run Git without ever loading more than ``max_bytes + 1`` stdout bytes."""
        try:
            with tempfile.TemporaryFile() as output:
                proc = subprocess.run(
                    ["git", "-C", str(root), *args],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    text=False,
                    check=False,
                    shell=False,
                    **get_subprocess_kwargs(),
                )
                if proc.returncode != 0:
                    self.raise_failure(proc, failure_class, change_set_id)
                total = output.tell()
                output.seek(0)
                data = output.read(max_bytes + 1)
                output.seek(0)
                digest = hashlib.sha256()
                while True:
                    chunk = output.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except AgentWorktreeError:
            raise
        except OSError as exc:
            raise AgentWorktreeError(
                failure_class,
                f"Git could not be started: {exc}",
                change_set_id=change_set_id,
            ) from exc
        truncated = total > max_bytes
        return data[:max_bytes], total, truncated, digest.hexdigest()

    def checked_out_worktree(
        self, root: Path, branch_ref: str, *, change_set_id: str
    ) -> str:
        """Return the path checking out *branch_ref*, or ``""`` when none does."""
        text = self.text(
            root,
            ["worktree", "list", "--porcelain"],
            change_set_id=change_set_id,
            failure_class="worktree_list_failed",
        )
        path = ""
        for line in text.splitlines():
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ").strip()
            elif line.startswith("branch "):
                ref = line.removeprefix("branch ").strip()
                if ref == branch_ref:
                    return path
            elif not line.strip():
                path = ""
        return ""

    @staticmethod
    def raise_failure(
        proc: subprocess.CompletedProcess[Any],
        failure_class: str,
        change_set_id: str,
        base_sha: str = "",
        recovery_path: str = "",
    ) -> None:
        stderr = (
            proc.stderr.decode("utf-8", "replace")
            if isinstance(proc.stderr, bytes)
            else str(proc.stderr or "")
        )
        stdout = (
            proc.stdout.decode("utf-8", "replace")
            if isinstance(proc.stdout, bytes)
            else str(proc.stdout or "")
        )
        detail = (stderr or stdout or f"Git exited with code {proc.returncode}").strip()
        raise AgentWorktreeError(
            failure_class,
            detail,
            change_set_id=change_set_id,
            base_sha=base_sha,
            recovery_path=recovery_path,
        )


__all__ = ["AgentWorktreeError", "GitRunner"]
