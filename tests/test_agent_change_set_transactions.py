"""Transactional truth and bounded discovery for retained Agent work."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from aura.agents.worktree import AgentWorktreeError, AgentWorktreeManager
from aura.agents.worktree_git import GitRunner
from aura.agents.worktree_records import WorktreeRecord
from aura.config import MAX_READ_BYTES
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root.resolve()


def _manager(repo: Path, tmp_path: Path) -> AgentWorktreeManager:
    return AgentWorktreeManager(repo, runtime_root=tmp_path / "runtime")


def _checkpoint(
    manager: AgentWorktreeManager, *, name: str = "change.txt", content: bytes = b"changed\n"
):
    worktree = manager.create("agent0001")
    (worktree.path / name).write_bytes(content)
    return manager.checkpoint(worktree)


def _approve(_request) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def test_fresh_turn_discovers_compact_change_sets_without_an_agent_roster(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    result = _checkpoint(manager)
    registry = ToolRegistry(repo)
    registry.set_agent_worktree_manager(manager)

    names = [tool["function"]["name"] for tool in registry.tool_defs()]
    listed = manager.list_change_sets()
    inspected = manager.inspect(result.change_set_id)

    assert "delegate_agent" not in names
    assert "list_agent_change_sets" in names
    assert "inspect_agent_change_set" in names
    assert listed["change_sets"][0]["change_set_id"] == result.change_set_id
    assert "diff" not in inspected
    assert inspected["changed_path_count"] == 1


def test_path_scoped_inspection_is_bounded_and_marks_binary_size_and_hash(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("agent0001")
    (worktree.path / "large.txt").write_text(
        "line with enough content to make a diff\n" * (MAX_READ_BYTES // 20),
        encoding="utf-8",
    )
    (worktree.path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    result = manager.checkpoint(worktree)

    inspected = manager.inspect(
        result.change_set_id, paths=("large.txt", "blob.bin")
    )
    files = {item["new_path"]: item for item in inspected["files"]}

    assert inspected["truncated"] is True
    assert inspected["diff_size_bytes"] > inspected["max_bytes"]
    assert len(inspected["diff_sha256"]) == 64
    assert "truncated at" in inspected["diff"]
    assert files["blob.bin"]["binary"] is True
    assert files["blob.bin"]["new"]["size_bytes"] == 300
    assert files["blob.bin"]["new"]["git_object_id"]
    assert files["blob.bin"]["new"]["hash_kind"] == "git_object_id"


def test_apply_releases_lock_for_approval_and_reports_cleanup_pending_truthfully(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(repo, tmp_path)
    result = _checkpoint(manager)
    observed: list[int] = []

    def fail_cleanup(_record, *, expected: str) -> None:
        raise AgentWorktreeError("change_set_cleanup_failed", "ref is locked")

    monkeypatch.setattr(manager, "_delete_exact_ref", fail_cleanup)

    def approve_while_observing(_request) -> ApprovalDecision:
        thread = threading.Thread(
            target=lambda: observed.append(manager.list_change_sets()["count"])
        )
        thread.start()
        thread.join(2)
        assert not thread.is_alive(), "manager lock remained held during approval"
        return ApprovalDecision(action="approve")

    applied = manager.apply(result.change_set_id, approval_cb=approve_while_observing)

    assert observed == [1]
    assert applied["applied"] is True
    assert applied["status"] == "applied_cleanup_pending"
    assert applied["cleanup_pending"] is True
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == result.result_sha
    pending = manager.list_change_sets()["change_sets"][0]
    assert pending["cleanup_pending"] is True


def test_apply_removes_exact_clean_retained_worktree_before_approval(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    result = _checkpoint(manager)
    record = manager._records[result.change_set_id]
    retained = manager._scope_dir(repo) / result.change_set_id
    _git(
        repo,
        "worktree",
        "add",
        str(retained),
        record.branch_ref.removeprefix("refs/heads/"),
    )
    record.worktree_path = str(retained)
    manager._save()

    def approve(request) -> ApprovalDecision:
        assert request.file_changes
        assert not retained.exists()
        assert record.worktree_path == ""
        return ApprovalDecision(action="approve")

    applied = manager.apply(result.change_set_id, approval_cb=approve)
    assert applied["applied"] is True


def test_apply_refuses_dirty_retained_worktree_before_touching_primary(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    result = _checkpoint(manager)
    record = manager._records[result.change_set_id]
    retained = manager._scope_dir(repo) / result.change_set_id
    _git(
        repo,
        "worktree",
        "add",
        str(retained),
        record.branch_ref.removeprefix("refs/heads/"),
    )
    record.worktree_path = str(retained)
    (retained / "dirty.txt").write_text("not checkpointed\n", encoding="utf-8")
    manager._save()
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(AgentWorktreeError) as caught:
        manager.apply(result.change_set_id, approval_cb=_approve)

    assert caught.value.failure_class == "worktree_cleanup_failed"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert retained.exists()


def test_apply_revalidates_primary_and_result_after_approval(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    result = _checkpoint(manager)

    def approve_then_dirty(_request) -> ApprovalDecision:
        (repo / "late.txt").write_text("arrived during approval\n", encoding="utf-8")
        return ApprovalDecision(action="approve")

    with pytest.raises(AgentWorktreeError) as caught:
        manager.apply(result.change_set_id, approval_cb=approve_then_dirty)

    assert caught.value.failure_class == "primary_worktree_dirty"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == result.base_sha
    assert manager.inspect(result.change_set_id)["result_sha"] == result.result_sha


def test_approval_material_omits_oversized_and_binary_blobs_explicitly(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("agent0001")
    (worktree.path / "large.txt").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    (worktree.path / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = manager.checkpoint(worktree)
    requests = []

    def reject(request) -> ApprovalDecision:
        requests.append(request)
        return ApprovalDecision(action="reject")

    response = manager.apply(result.change_set_id, approval_cb=reject)
    changes = {change.rel_path: change for change in requests[0].file_changes}

    assert response["applied"] is False
    assert "text content omitted" in changes["large.txt"].new_content
    assert "size_bytes=" in changes["large.txt"].new_content
    assert "git_object_id=" in changes["large.txt"].new_content
    assert "truncated=true" in changes["large.txt"].new_content
    assert "binary content" in changes["blob.bin"].new_content
    assert "loaded=false" in changes["blob.bin"].new_content


def test_discard_checkpoints_stranded_edits_before_lock_free_approval(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("agent0001")
    (worktree.path / "stranded.txt").write_text("valuable edits\n", encoding="utf-8")
    approvals = []

    def approve(request) -> ApprovalDecision:
        approvals.append(request)
        observed: list[int] = []
        thread = threading.Thread(
            target=lambda: observed.append(manager.list_change_sets()["count"])
        )
        thread.start()
        thread.join(2)
        assert observed == [1]
        return ApprovalDecision(action="approve")

    discarded = manager.discard(worktree.change_set_id, approval_cb=approve)

    assert discarded["discarded"] is True
    assert approvals[0].file_changes[0].rel_path == "stranded.txt"
    assert approvals[0].file_changes[0].new_content == "valuable edits\n"


def test_first_discard_handles_recovery_record_with_empty_worktree_path(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    change_set_id = "aw-stranded-empty"
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    primary_ref = _git(repo, "symbolic-ref", "HEAD").stdout.strip()
    scope = manager._scope_dir(repo)
    branch_ref = f"refs/heads/aura/agent/{scope.name}/{change_set_id}"
    _git(repo, "update-ref", branch_ref, base)
    manager._records[change_set_id] = WorktreeRecord(
        change_set_id=change_set_id,
        agent_id="agent0001",
        workspace_root=str(repo),
        base_sha=base,
        primary_ref=primary_ref,
        branch_ref=branch_ref,
        worktree_path="",
        state="recovery",
    )
    manager._save()

    discarded = manager.discard(change_set_id, approval_cb=_approve)

    assert discarded["discarded"] is True
    assert not Path("").resolve().name == change_set_id
    assert _git(repo, "show-ref", "--verify", branch_ref, check=False).returncode != 0


def test_branch_checked_out_elsewhere_is_never_deleted(repo: Path, tmp_path: Path) -> None:
    manager = _manager(repo, tmp_path)
    result = _checkpoint(manager)
    record = manager._records[result.change_set_id]
    retained = tmp_path / "checked-out"
    _git(
        repo,
        "worktree",
        "add",
        str(retained),
        record.branch_ref.removeprefix("refs/heads/"),
    )
    record.worktree_path = ""  # stale record must not bypass Git's checked-out fact
    manager._save()

    with pytest.raises(AgentWorktreeError) as caught:
        manager.discard(result.change_set_id, approval_cb=_approve)

    assert caught.value.failure_class == "change_set_ref_checked_out"
    assert retained.exists()
    assert _git(repo, "show-ref", "--verify", record.branch_ref).returncode == 0


def test_missing_result_ref_is_distinct_from_other_git_failures(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(repo, tmp_path)
    result = _checkpoint(manager)
    record = manager._records[result.change_set_id]
    _git(repo, "update-ref", "-d", record.branch_ref, result.result_sha)

    with pytest.raises(AgentWorktreeError) as missing:
        manager.inspect(result.change_set_id)
    assert missing.value.failure_class == "change_set_ref_missing"

    runner = GitRunner()
    failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal")
    monkeypatch.setattr("aura.agents.worktree_git.subprocess.run", lambda *_a, **_k: failed)
    with pytest.raises(AgentWorktreeError) as other:
        runner.ref_sha(repo, "refs/heads/nope")
    assert other.value.failure_class == "git_revision_failed"


def test_recovery_inspection_surfaces_git_failure(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("agent0001")
    (worktree.path / "partial.txt").write_text("partial\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise AgentWorktreeError(
            "change_set_inspection_failed", "git status failed",
            change_set_id=worktree.change_set_id,
        )

    monkeypatch.setattr(manager._git, "bounded_bytes", fail)
    with pytest.raises(AgentWorktreeError) as caught:
        manager.inspect(worktree.change_set_id)
    assert caught.value.failure_class == "change_set_inspection_failed"
