"""Writable Agent worktrees: isolation, retention, and root decisions."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentDefinition
from aura.agents.roster import AgentRosterEntry, AgentTurnRoster
from aura.agents.runtime import AgentDelegationRunner, _reported_tests
from aura.agents.worktree import AgentWorktreeError, AgentWorktreeManager
from aura.client import ContentDelta, Done
from aura.conversation.history import History
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.catalog import child_agent_tool_defs
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


def _approve(_request) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "delete.txt").write_text("delete me\n", encoding="utf-8")
    (root / "rename.txt").write_text("rename me\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root.resolve()


def _manager(repo: Path, tmp_path: Path) -> AgentWorktreeManager:
    return AgentWorktreeManager(repo, runtime_root=tmp_path / "runtime")


def test_writable_delegation_requires_a_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()

    with pytest.raises(AgentWorktreeError) as caught:
        AgentWorktreeManager(root, runtime_root=tmp_path / "runtime").create("agent")

    assert caught.value.failure_class == "git_repository_required"
    assert caught.value.change_set_id.startswith("aw-")


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_every_kind_of_primary_dirt_is_refused(
    repo: Path, tmp_path: Path, kind: str
) -> None:
    if kind == "staged":
        (repo / "keep.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "keep.txt")
    elif kind == "unstaged":
        (repo / "keep.txt").write_text("unstaged\n", encoding="utf-8")
    else:
        (repo / "new.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(AgentWorktreeError) as caught:
        _manager(repo, tmp_path).create("agent")

    assert caught.value.failure_class == "primary_worktree_dirty"
    assert "staged, unstaged, or untracked" in str(caught.value)


def test_each_invocation_gets_a_unique_short_owned_worktree(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    first = manager.create("agent")
    first_result = manager.checkpoint(first)
    second = manager.create("agent")
    second_result = manager.checkpoint(second)

    assert first.change_set_id != second.change_set_id
    assert first.path != second.path
    assert len(first.path.name) == len("aw-") + 16
    assert first_result.result_sha == first.base_sha
    assert second_result.result_sha == second.base_sha
    assert not first.path.exists()
    assert not second.path.exists()


def test_read_only_gets_no_write_or_terminal_tool_at_all() -> None:
    names = [tool["function"]["name"] for tool in child_agent_tool_defs()]

    assert "apply_patch" not in names
    assert "shell" not in names


def test_read_write_is_one_grant_carrying_both_edit_and_terminal() -> None:
    """Editing in an isolated worktree and running commands there are one
    choice now, so the tool surface must not pretend they can be separated."""
    names = [
        tool["function"]["name"]
        for tool in child_agent_tool_defs(AgentPermission.READ_WRITE)
    ]

    assert "apply_patch" in names
    assert "shell" in names


def test_terminal_validation_results_are_reported_structurally() -> None:
    history = History()
    history.append_assistant(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("test", "shell", command="pytest -q")],
        }
    )
    history.append_tool_result(
        "test",
        json.dumps(
            {
                "ok": True,
                "command": "python -m pytest -q",
                "working_directory": "",
                "exit_code": 0,
                "validation_classification": "passed",
            }
        ),
    )

    assert _reported_tests(history) == (
        {
            "command": "python -m pytest -q",
            "cwd": "",
            "ok": True,
            "exit_code": 0,
            "classification": "passed",
        },
    )


def test_isolated_file_tool_enforces_paths_and_protected_internals(
    repo: Path, tmp_path: Path
) -> None:
    worktree = _manager(repo, tmp_path).create("agent")
    registry = ToolRegistry(
        worktree.path, read_only=False, isolated_agent=True
    )

    for path in (
        "../escape.txt",
        ".git",
        ".git/config",
        ".aura/settings/x",
        str(worktree.path / ".aura" / "absolute.txt"),
    ):
        result = registry.execute(
            "apply_patch",
            {"operation": "create", "path": path, "content": "no"},
            _approve,
        )
        assert result.ok is False
        assert result.payload["failure_class"] in {
            "agent_workspace_escape",
            "agent_protected_path",
        }

    good = registry.execute(
        "apply_patch",
        {"operation": "create", "path": "src/new.txt", "content": "yes\n"},
        _approve,
    )
    assert good.ok is True
    assert (worktree.path / "src" / "new.txt").read_text(encoding="utf-8") == "yes\n"
    edited = registry.execute(
        "apply_patch",
        {"operation": "replace", "path": "keep.txt", "content": "edited\n"},
        _approve,
    )
    deleted = registry.execute(
        "apply_patch",
        {"operation": "delete", "path": "delete.txt", "reason": "assigned change"},
        _approve,
    )
    assert edited.ok is True
    assert deleted.ok is True
    assert (worktree.path / "keep.txt").read_text(encoding="utf-8") == "edited\n"
    assert not (worktree.path / "delete.txt").exists()


def test_isolated_file_tool_preserves_stale_target_checks(
    repo: Path, tmp_path: Path
) -> None:
    worktree = _manager(repo, tmp_path).create("agent")
    registry = ToolRegistry(worktree.path, read_only=False, isolated_agent=True)

    def change_after_proposal(_request):
        (worktree.path / "keep.txt").write_text("raced\n", encoding="utf-8")
        return ApprovalDecision("approve")

    result = registry.execute(
        "apply_patch",
        {"operation": "replace", "path": "keep.txt", "content": "agent\n"},
        change_after_proposal,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "stale_approval"
    assert (worktree.path / "keep.txt").read_text(encoding="utf-8") == "raced\n"


def test_isolated_file_tool_rejects_a_symlink_escape(repo: Path, tmp_path: Path) -> None:
    worktree = _manager(repo, tmp_path).create("agent")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (worktree.path / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    registry = ToolRegistry(worktree.path, read_only=False, isolated_agent=True)

    result = registry.execute(
        "apply_patch",
        {"operation": "create", "path": "escape/no.txt", "content": "no\n"},
        _approve,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "agent_workspace_escape"
    assert not (outside / "no.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="junctions exist only on Windows")
def test_isolated_file_tool_rejects_a_junction_escape(repo: Path, tmp_path: Path) -> None:
    worktree = _manager(repo, tmp_path).create("agent")
    outside = tmp_path / "junction-outside"
    outside.mkdir()
    link = worktree.path / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {created.stderr or created.stdout}")
    registry = ToolRegistry(worktree.path, read_only=False, isolated_agent=True)

    result = registry.execute(
        "apply_patch",
        {"operation": "create", "path": "junction/no.txt", "content": "no\n"},
        _approve,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "agent_workspace_escape"
    assert not (outside / "no.txt").exists()


def test_checkpoint_retains_new_deleted_renamed_and_binary_files(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "new.txt").write_text("new\n", encoding="utf-8")
    (worktree.path / "delete.txt").unlink()
    (worktree.path / "rename.txt").rename(worktree.path / "renamed.txt")
    binary = bytes(range(256)) + b"\x00\xff"
    (worktree.path / "image.bin").write_bytes(binary)

    result = manager.checkpoint(worktree)

    assert result.status == "ready"
    assert result.result_sha != result.base_sha
    assert {"new.txt", "delete.txt", "rename.txt", "renamed.txt", "image.bin"} <= set(
        result.changed_paths
    )
    assert "files changed" in result.diffstat
    assert not worktree.path.exists()
    assert _git(repo, "show", f"{result.result_sha}:image.bin", check=True).stdout.encode(
        "utf-8", "replace"
    ) != b""  # existence; exact bytes are checked after application below


def test_checkpoint_squashes_child_commits_into_one_runtime_commit(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("terminal-writer")
    (worktree.path / "first.txt").write_text("first\n", encoding="utf-8")
    _git(worktree.path, "add", "first.txt")
    _git(
        worktree.path,
        "-c",
        "user.name=Child",
        "-c",
        "user.email=child@example.com",
        "commit",
        "-m",
        "child intermediate",
    )
    (worktree.path / "second.txt").write_text("second\n", encoding="utf-8")

    result = manager.checkpoint(worktree)

    commits = _git(repo, "rev-list", f"{result.base_sha}..{result.result_sha}").stdout.splitlines()
    parent = _git(repo, "rev-parse", f"{result.result_sha}^").stdout.strip()
    subject = _git(repo, "show", "-s", "--format=%s", result.result_sha).stdout.strip()
    assert commits == [result.result_sha]
    assert parent == result.base_sha
    assert subject == f"Aura agent change set {result.change_set_id}"


def test_inspection_is_observational_and_identifies_binary_changes(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "keep.txt").write_text("changed\n", encoding="utf-8")
    (worktree.path / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = manager.checkpoint(worktree)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    inspected = manager.inspect(result.change_set_id, paths=("keep.txt", "blob.bin"))

    assert inspected["ok"] is True
    assert inspected["base_sha"] == before
    assert "keep.txt" in inspected["diff"]
    assert "Binary files" in inspected["diff"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_minimal_lifecycle_record_survives_manager_restart(
    repo: Path, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    manager = AgentWorktreeManager(repo, runtime_root=runtime)
    worktree = manager.create("writer")
    (worktree.path / "retained.txt").write_text("retained\n", encoding="utf-8")
    result = manager.checkpoint(worktree)

    restarted = AgentWorktreeManager(repo, runtime_root=runtime)

    inspected = restarted.inspect(result.change_set_id)
    assert inspected["result_sha"] == result.result_sha
    assert inspected["changed_paths"] == ["retained.txt"]


def test_apply_is_approved_and_fast_forwards_exact_result(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "keep.txt").write_text("applied\n", encoding="utf-8")
    binary = b"\x00\xff\x10binary"
    (worktree.path / "asset.bin").write_bytes(binary)
    result = manager.checkpoint(worktree)
    requests = []

    applied = manager.apply(
        result.change_set_id,
        approval_cb=lambda request: requests.append(request) or ApprovalDecision("approve"),
    )

    assert applied["applied"] is True
    assert requests and requests[0].tool_name == "apply_agent_change_set"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == result.result_sha
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "applied\n"
    assert (repo / "asset.bin").read_bytes() == binary
    with pytest.raises(AgentWorktreeError, match="No unresolved"):
        manager.inspect(result.change_set_id)


@pytest.mark.parametrize("primary_change", ["moved", "dirty"])
def test_apply_refuses_moved_or_dirty_primary_and_preserves_result(
    repo: Path, tmp_path: Path, primary_change: str
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "keep.txt").write_text("agent\n", encoding="utf-8")
    result = manager.checkpoint(worktree)
    if primary_change == "moved":
        (repo / "keep.txt").write_text("primary\n", encoding="utf-8")
        _git(repo, "add", "keep.txt")
        _git(repo, "commit", "-m", "primary moved")
        expected = "primary_branch_moved"
    else:
        (repo / "keep.txt").write_text("dirty\n", encoding="utf-8")
        expected = "primary_worktree_dirty"
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(AgentWorktreeError) as caught:
        manager.apply(result.change_set_id, approval_cb=_approve)

    assert caught.value.failure_class == expected
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert manager.inspect(result.change_set_id)["result_sha"] == result.result_sha


def test_apply_refuses_a_different_primary_branch_even_at_the_same_sha(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "keep.txt").write_text("agent\n", encoding="utf-8")
    result = manager.checkpoint(worktree)
    _git(repo, "switch", "-c", "other-primary")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(AgentWorktreeError) as caught:
        manager.apply(result.change_set_id, approval_cb=_approve)

    assert caught.value.failure_class == "primary_branch_moved"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert manager.inspect(result.change_set_id)["ok"] is True


def test_rejected_application_leaves_both_sides_untouched(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "keep.txt").write_text("agent\n", encoding="utf-8")
    result = manager.checkpoint(worktree)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    refused = manager.apply(
        result.change_set_id,
        approval_cb=lambda _request: ApprovalDecision("reject"),
    )

    assert refused["applied"] is False
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert manager.inspect(result.change_set_id)["ok"] is True


def test_discard_removes_only_the_exact_owned_result(repo: Path, tmp_path: Path) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "new.txt").write_text("discard\n", encoding="utf-8")
    result = manager.checkpoint(worktree)
    unrelated = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "branch", "keep-this-branch", unrelated)

    discarded = manager.discard(result.change_set_id, approval_cb=_approve)

    assert discarded["discarded"] is True
    assert _git(repo, "show-ref", "--verify", "refs/heads/keep-this-branch").returncode == 0
    assert _git(repo, "status", "--porcelain").stdout == ""
    with pytest.raises(AgentWorktreeError):
        manager.inspect(result.change_set_id)


def test_discard_refuses_to_displace_another_active_lifecycle(
    repo: Path, tmp_path: Path
) -> None:
    """Discarding a stranded result may not take the one lifecycle slot.

    Claiming the slot is how ``checkpoint`` proves nothing else owns this
    repository's writable state, and releasing it is what makes a second
    writable creation permissible again. A record stranded by an earlier
    process is reloaded with the slot free, so a later run can legitimately
    hold it — and discarding the stranded one must not clear that run's marker
    on release.
    """
    first = _manager(repo, tmp_path)
    stranded = first.create("writer")
    (stranded.path / "partial.txt").write_text("partial\n", encoding="utf-8")

    # A fresh manager over the same runtime state: the stranded record is
    # reloaded, but nothing holds the lifecycle slot, so a new run may start.
    reloaded = _manager(repo, tmp_path)
    running = reloaded.create("other-writer")

    with pytest.raises(AgentWorktreeError) as caught:
        reloaded.discard(stranded.change_set_id, approval_cb=_approve)

    assert caught.value.failure_class == "writable_delegation_busy"
    # The running lifecycle still owns the slot, and nothing was removed.
    assert reloaded._active_id == running.change_set_id
    assert (stranded.path / "partial.txt").is_file()
    assert reloaded.inspect(stranded.change_set_id)["ok"] is True


def test_cancellation_recovery_checkpoints_stable_partial_edits(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "partial.txt").write_text("stable before stop\n", encoding="utf-8")

    recovered = manager.recover(worktree)

    assert recovered.status == "ready"
    assert recovered.result_sha
    assert "partial.txt" in recovered.changed_paths
    assert not worktree.path.exists()
    assert manager.inspect(recovered.change_set_id)["ok"] is True


def test_workspace_switch_waits_for_active_cancellation_recovery(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "partial.txt").write_text("preserve before switch\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init")
    _git(other, "config", "user.name", "Test User")
    _git(other, "config", "user.email", "test@example.com")
    (other / "other.txt").write_text("other\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "other base")

    manager.set_workspace_root(other)
    recovered = manager.recover(worktree)

    assert recovered.result_sha
    assert manager.workspace_root == other.resolve()
    manager.set_workspace_root(repo)
    assert manager.inspect(recovered.change_set_id)["ok"] is True


def test_cleanup_failure_preserves_the_recovery_worktree_and_ref(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(repo, tmp_path)
    worktree = manager.create("writer")
    (worktree.path / "partial.txt").write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(manager, "_cleanup_clean_worktree", lambda _record: "locked")

    result = manager.checkpoint(worktree)

    assert result.result_sha
    assert result.failure_class == "worktree_cleanup_failed"
    assert result.worktree_path == str(worktree.path)
    assert worktree.path.exists()
    assert manager.inspect(result.change_set_id)["ok"] is True


def test_writable_runner_returns_the_complete_change_set_shape(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda provider=None: True
    )
    backend = _Backend(
        [
            [
                Done(
                    finish_reason="tool_calls",
                    full_message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            _tool_call(
                                "edit",
                                "apply_patch",
                                operation="create",
                                path="from-agent.txt",
                                content="isolated\n",
                            )
                        ],
                    },
                )
            ],
            [
                ContentDelta(text="Created from-agent.txt. Tests: not run."),
                Done(
                    finish_reason="stop",
                    full_message={
                        "role": "assistant",
                        "content": "Created from-agent.txt. Tests: not run.",
                    },
                ),
            ],
        ]
    )
    manager = _manager(repo, tmp_path)
    runner = AgentDelegationRunner(
        workspace_root=repo,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        backend_factory=lambda _provider: backend,
        worktree_manager=manager,
    )

    result = runner.run(_entry(AgentPermission.READ_WRITE), "Create the file.")
    payload = result.payload()

    assert payload["status"] == "completed"
    assert payload["agent_id"] == "writer-agent"
    assert payload["change_set_id"].startswith("aw-")
    assert payload["base_sha"]
    assert payload["result_sha"] != payload["base_sha"]
    assert payload["changed_paths"] == ["from-agent.txt"]
    assert payload["diffstat"]
    assert payload["tests_reported"] == []
    assert payload["result"] == "Created from-agent.txt. Tests: not run."
    assert "final_report" not in payload
    assert not (repo / "from-agent.txt").exists()
    first_names = [tool["function"]["name"] for tool in backend.requests[0]["tools"]]
    assert "apply_patch" in first_names
    assert "shell" in first_names
    assert manager.inspect(payload["change_set_id"])["ok"] is True


def test_cancelled_runner_checkpoints_partial_edits_after_stopping(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda provider=None: True
    )
    cancel = threading.Event()
    backend = _CancellingBackend(cancel)
    manager = _manager(repo, tmp_path)
    runner = AgentDelegationRunner(
        workspace_root=repo,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        backend_factory=lambda _provider: backend,
        worktree_manager=manager,
    )

    result = runner.run(
        _entry(AgentPermission.READ_WRITE),
        "Start the edit.",
        cancel_event=cancel,
    )

    assert result.status.value == "cancelled"
    assert result.result_sha
    assert result.changed_paths == ("partial.txt",)
    assert not (repo / "partial.txt").exists()
    assert manager.inspect(result.change_set_id)["ok"] is True


def test_root_change_set_operations_are_never_exposed_to_a_child(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    registry = ToolRegistry(repo)
    registry.set_agent_worktree_manager(manager)
    registry.set_turn_agent_roster(
        AgentTurnRoster(entries=(_entry(AgentPermission.READ_WRITE),))
    )
    root_names = [tool["function"]["name"] for tool in registry.tool_defs()]
    child_names = [
        tool["function"]["name"]
        for tool in child_agent_tool_defs(AgentPermission.READ_WRITE)
    ]

    assert {
        "inspect_agent_change_set",
        "apply_agent_change_set",
        "discard_agent_change_set",
    } <= set(root_names)
    assert not {
        "inspect_agent_change_set",
        "apply_agent_change_set",
        "discard_agent_change_set",
    } & set(child_names)


def test_read_only_root_can_inspect_but_not_apply_or_discard(
    repo: Path, tmp_path: Path
) -> None:
    manager = _manager(repo, tmp_path)
    registry = ToolRegistry(repo, read_only=True)
    registry.set_agent_worktree_manager(manager)
    registry.set_turn_agent_roster(
        AgentTurnRoster(entries=(_entry(AgentPermission.READ_WRITE),))
    )

    names = [tool["function"]["name"] for tool in registry.tool_defs()]

    assert "inspect_agent_change_set" in names
    assert "apply_agent_change_set" not in names
    assert "discard_agent_change_set" not in names


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="writer-agent",
        scope=AgentScope.PROJECT,
        name="Writer",
        description="Writes a focused change.",
        instructions="Implement the assigned change and report tests.",
    )


def _entry(permission: AgentPermission) -> AgentRosterEntry:
    return AgentRosterEntry(definition=_definition(), permission=permission)


def _tool_call(call_id: str, name: str, **args) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _Backend:
    def __init__(self, rounds: list[list[object]]) -> None:
        self.rounds = rounds
        self.requests: list[dict] = []

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        yield from self.rounds[len(self.requests) - 1]


class _CancellingBackend:
    def __init__(self, cancel: threading.Event) -> None:
        self.cancel = cancel
        self.requests = 0

    def stream(self, **_kwargs):
        self.requests += 1
        if self.requests == 1:
            yield Done(
                finish_reason="tool_calls",
                full_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        _tool_call(
                            "partial",
                            "apply_patch",
                            operation="create",
                            path="partial.txt",
                            content="partial\n",
                        )
                    ],
                },
            )
            return
        self.cancel.set()
        if False:  # make this branch a generator without yielding an event
            yield None
