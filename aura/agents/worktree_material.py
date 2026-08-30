"""Bounded change-set inspection and approval materialization."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from aura.agents.worktree_git import AgentWorktreeError, GitRunner
from aura.agents.worktree_records import WorktreeRecord
from aura.config import MAX_READ_BYTES
from aura.conversation.tools._types import ApprovalFileChange, ApprovalRequest

MAX_INSPECTION_PATHS = 20
MAX_DIFFSTAT_BYTES = min(MAX_READ_BYTES, 16 * 1024)


class ChangeSetMaterializer:
    """Turn stable Git objects into compact, explicitly bounded observations."""

    def __init__(self, git: GitRunner) -> None:
        self._git = git

    def name_status(self, record: WorktreeRecord) -> list[tuple[str, str, str]]:
        data, total, truncated, digest = self._git.bounded_bytes(
            Path(record.workspace_root),
            ["diff", "--name-status", "-z", "-M", record.base_sha, record.result_sha, "--"],
            max_bytes=MAX_READ_BYTES,
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        )
        if truncated:
            raise AgentWorktreeError(
                "change_set_metadata_too_large",
                "The changed-path metadata exceeds Aura's bounded inspection "
                f"limit (size_bytes={total}; max_bytes={MAX_READ_BYTES}; "
                f"sha256={digest}; truncated=true). The retained work was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
                recovery_path=record.worktree_path,
            )
        tokens = [
            item.decode("utf-8", "surrogateescape")
            for item in data.split(b"\0")
            if item
        ]
        rows: list[tuple[str, str, str]] = []
        index = 0
        while index < len(tokens):
            status = tokens[index][:1]
            index += 1
            if status in ("R", "C") and index + 1 < len(tokens):
                old_path, new_path = tokens[index], tokens[index + 1]
                index += 2
            elif index < len(tokens):
                old_path = new_path = tokens[index]
                index += 1
            else:
                break
            rows.append((status, old_path, new_path))
        return rows

    def changed_paths(
        self, root: Path, base: str, result: str, change_set_id: str
    ) -> tuple[str, ...]:
        record = WorktreeRecord(
            change_set_id, "", str(root), base, "", "", "", result_sha=result
        )
        paths: list[str] = []
        for _status, old_path, new_path in self.name_status(record):
            for path in (old_path, new_path):
                if path and path not in paths:
                    paths.append(path)
        return tuple(paths)

    def diffstat(
        self, root: Path, base: str, result: str, change_set_id: str
    ) -> str:
        data, total, truncated, digest = self._git.bounded_bytes(
            root,
            ["diff", "--stat", "--summary", "--find-renames", base, result, "--"],
            max_bytes=MAX_DIFFSTAT_BYTES,
            change_set_id=change_set_id,
            failure_class="checkpoint_failed",
        )
        text = data.decode("utf-8", "replace").strip()
        if truncated:
            text += (
                f"\n... [truncated at {MAX_DIFFSTAT_BYTES} bytes; full_size={total}; "
                f"sha256={digest}]"
            )
        return text

    def inspect_diff(
        self, record: WorktreeRecord, requested_paths: tuple[str, ...]
    ) -> dict[str, Any]:
        paths = self._validated_paths(record, requested_paths)
        args = [
            "diff",
            "--find-renames",
            "--no-ext-diff",
            record.base_sha,
            record.result_sha,
            "--",
            *paths,
        ]
        data, total, truncated, digest = self._git.bounded_bytes(
            Path(record.workspace_root),
            args,
            max_bytes=MAX_READ_BYTES,
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        )
        text = data.decode("utf-8", "replace")
        if truncated:
            text = text.rstrip() + (
                f"\n... [truncated at {MAX_READ_BYTES} bytes; "
                f"full_size={total}; sha256={digest}]\n"
            )
        return {
            "paths": list(paths),
            "diff": text,
            "diff_size_bytes": total,
            "diff_sha256": digest,
            "truncated": truncated,
            "max_bytes": MAX_READ_BYTES,
        }

    def file_metadata(
        self, record: WorktreeRecord, *, paths: set[str] | None = None
    ) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for status, old_path, new_path in self.name_status(record):
            if paths is not None and old_path not in paths and new_path not in paths:
                continue
            binary = self._is_binary(record, new_path or old_path)
            files.append(
                {
                    "status": status,
                    "old_path": old_path,
                    "new_path": new_path,
                    "binary": binary,
                    "old": self._blob_metadata(record, record.base_sha, old_path)
                    if status != "A" else None,
                    "new": self._blob_metadata(record, record.result_sha, new_path)
                    if status != "D" else None,
                }
            )
        return files

    def approval_request(
        self, record: WorktreeRecord, *, discard: bool = False
    ) -> ApprovalRequest:
        changes = self._approval_changes(record)
        if not changes:
            changes = (
                ApprovalFileChange(
                    f"Agent change set {record.change_set_id}",
                    "",
                    record.diffstat,
                    False,
                ),
            )
        first = changes[0]
        return ApprovalRequest(
            tool_name="discard_agent_change_set" if discard else "apply_agent_change_set",
            rel_path=first.rel_path,
            old_content=first.old_content,
            new_content=first.new_content,
            is_new_file=first.is_new_file,
            changes=changes,
        )

    def _approval_changes(
        self, record: WorktreeRecord
    ) -> tuple[ApprovalFileChange, ...]:
        if not record.result_sha:
            return ()
        remaining = [MAX_READ_BYTES]
        changes: list[ApprovalFileChange] = []
        for status, old_path, new_path in self.name_status(record):
            binary = self._is_binary(record, new_path or old_path)
            old = (
                self._approval_blob(record, record.base_sha, old_path, binary, remaining)
                if status != "A" else ""
            )
            new = (
                self._approval_blob(record, record.result_sha, new_path, binary, remaining)
                if status != "D" else ""
            )
            if status == "R":
                changes.append(ApprovalFileChange(old_path, old, "", False, "delete"))
                changes.append(ApprovalFileChange(new_path, "", new, True, "create"))
                continue
            if status == "C":
                changes.append(ApprovalFileChange(new_path, "", new, True, "create"))
                continue
            rel_path = f"{old_path} -> {new_path}" if old_path != new_path else new_path
            action = "create" if status == "A" else "delete" if status == "D" else "modify"
            changes.append(
                ApprovalFileChange(rel_path, old, new, status == "A", action)
            )
        return tuple(changes)

    def _approval_blob(
        self,
        record: WorktreeRecord,
        sha: str,
        path: str,
        binary: bool,
        remaining: list[int],
    ) -> str:
        meta = self._blob_metadata(record, sha, path)
        size = int(meta["size_bytes"])
        object_id = str(meta["git_object_id"])
        if binary:
            return (
                f"[binary content; size_bytes={size}; git_object_id={object_id}; "
                "loaded=false; truncated=false]"
            )
        if size > remaining[0]:
            return (
                f"[text content omitted; size_bytes={size}; git_object_id={object_id}; "
                f"loaded=false; truncated=true; approval_budget={MAX_READ_BYTES}]"
            )
        proc = self._git.run(
            Path(record.workspace_root),
            ["show", f"{sha}:{path}"],
            text=False,
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        )
        data = bytes(proc.stdout or b"")
        if len(data) != size or len(data) > remaining[0]:
            return (
                f"[content changed while materializing; size_bytes={size}; "
                f"git_object_id={object_id}; loaded=false; truncated=true]"
            )
        remaining[0] -= len(data)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return (
                f"[binary content; size_bytes={size}; git_object_id={object_id}; "
                "loaded=false; truncated=false]"
            )

    def _blob_metadata(
        self, record: WorktreeRecord, sha: str, path: str
    ) -> dict[str, Any]:
        root = Path(record.workspace_root)
        spec = f"{sha}:{path}"
        object_id = self._git.text(
            root,
            ["rev-parse", "--verify", spec],
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        ).strip()
        size_text = self._git.text(
            root,
            ["cat-file", "-s", spec],
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        ).strip()
        try:
            size = int(size_text)
        except ValueError as exc:
            raise AgentWorktreeError(
                "change_set_inspection_failed",
                f"Git returned an invalid blob size for {path!r}.",
                change_set_id=record.change_set_id,
            ) from exc
        return {
            "size_bytes": size,
            "git_object_id": object_id,
            "hash_kind": "git_object_id",
        }

    def _is_binary(self, record: WorktreeRecord, path: str) -> bool:
        text = self._git.text(
            Path(record.workspace_root),
            ["diff", "--numstat", record.base_sha, record.result_sha, "--", path],
            change_set_id=record.change_set_id,
            failure_class="change_set_inspection_failed",
        )
        return any(line.startswith("-\t-\t") for line in text.splitlines())

    @staticmethod
    def _validated_paths(
        record: WorktreeRecord, requested: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not requested:
            raise AgentWorktreeError(
                "change_set_path_required",
                "Choose one or more changed paths for bounded diff inspection.",
                change_set_id=record.change_set_id,
            )
        if len(requested) > MAX_INSPECTION_PATHS:
            raise AgentWorktreeError(
                "change_set_path_limit",
                f"Inspect at most {MAX_INSPECTION_PATHS} paths at once.",
                change_set_id=record.change_set_id,
            )
        changed = set(record.changed_paths)
        clean: list[str] = []
        for raw in requested:
            path = str(raw or "").replace("\\", "/").strip()
            pure = PurePosixPath(path)
            if (
                not path
                or pure.is_absolute()
                or ".." in pure.parts
                or path not in changed
            ):
                raise AgentWorktreeError(
                    "change_set_path_invalid",
                    f"'{raw}' is not an exact changed path in this change set.",
                    change_set_id=record.change_set_id,
                )
            if path not in clean:
                clean.append(path)
        return tuple(clean)


__all__ = [
    "MAX_DIFFSTAT_BYTES",
    "MAX_INSPECTION_PATHS",
    "ChangeSetMaterializer",
]
