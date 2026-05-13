"""Tests for Aura's source-install git updater."""

from __future__ import annotations

from pathlib import Path

import aura.updater as updater
from aura.updater import GitCommandResult, get_app_repo_root, get_update_status, pull_latest


def test_get_app_repo_root_walks_up_from_package(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "aura"
    package.mkdir(parents=True)
    (repo / ".git").mkdir()
    module_file = package / "updater.py"
    module_file.write_text("# test", encoding="utf-8")

    monkeypatch.setattr(updater, "__file__", str(module_file))

    assert get_app_repo_root() == repo


def test_get_app_repo_root_returns_none_without_git(monkeypatch, tmp_path: Path) -> None:
    package = tmp_path / "install" / "aura"
    package.mkdir(parents=True)
    module_file = package / "updater.py"
    module_file.write_text("# test", encoding="utf-8")

    monkeypatch.setattr(updater, "__file__", str(module_file))

    assert get_app_repo_root() is None


def test_get_update_status_reports_not_git(monkeypatch) -> None:
    monkeypatch.setattr(updater, "get_app_repo_root", lambda: None)

    status = get_update_status()

    assert status.is_git_repo is False
    assert status.state == "not_git"
    assert "source installs" in status.message


def test_get_update_status_reports_no_upstream(monkeypatch, tmp_path: Path) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 128, "", "fatal: no upstream")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "no_upstream"
    assert status.branch == "main"
    assert status.commit == "abc1234"
    assert status.upstream is None
    assert status.can_pull is False


def test_get_update_status_classifies_behind_and_clean(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["fetch"]:
            return GitCommandResult(["git", *args], 0, "fetch output\n", "")
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return GitCommandResult(["git", *args], 0, "0\t2\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "behind"
    assert status.ahead == 0
    assert status.behind == 2
    assert status.has_local_changes is False
    assert status.can_pull is True


def test_get_update_status_disables_pull_with_local_changes(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 0, " M aura/updater.py\n", "")
        if args == ["fetch"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return GitCommandResult(["git", *args], 0, "0 1\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "behind"
    assert status.has_local_changes is True
    assert status.can_pull is False
    assert "Commit, stash, or discard" in status.message


def test_get_update_status_fails_closed_when_status_fails(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 128, "", "fatal: status failed")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "error"
    assert status.can_pull is False
    assert "local changes" in status.message


def test_get_update_status_classifies_diverged(monkeypatch, tmp_path: Path) -> None:
    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return GitCommandResult(["git", *args], 0, "true\n", "")
        if args == ["branch", "--show-current"]:
            return GitCommandResult(["git", *args], 0, "main\n", "")
        if args == ["rev-parse", "--short", "HEAD"]:
            return GitCommandResult(["git", *args], 0, "abc1234\n", "")
        if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return GitCommandResult(["git", *args], 0, "origin/main\n", "")
        if args == ["status", "--porcelain"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["fetch"]:
            return GitCommandResult(["git", *args], 0, "", "")
        if args == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return GitCommandResult(["git", *args], 0, "1 2\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "run_git_command", fake_git)

    status = get_update_status(tmp_path)

    assert status.state == "diverged"
    assert status.can_pull is False


def test_pull_latest_refuses_local_changes(monkeypatch, tmp_path: Path) -> None:
    status = updater.UpdateStatus(
        tmp_path,
        True,
        branch="main",
        commit="abc1234",
        upstream="origin/main",
        state="behind",
        behind=1,
        has_local_changes=True,
    )
    monkeypatch.setattr(updater, "get_update_status", lambda *a, **k: status)

    result = pull_latest(tmp_path)

    assert result.success is False
    assert "Local changes" in result.message


def test_pull_latest_runs_ff_only_when_safe(monkeypatch, tmp_path: Path) -> None:
    status = updater.UpdateStatus(
        tmp_path,
        True,
        branch="main",
        commit="abc1234",
        upstream="origin/main",
        state="behind",
        behind=1,
    )
    calls: list[list[str]] = []

    def fake_git(repo_root: Path, args: list[str], **kwargs: object) -> GitCommandResult:
        calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            sha = "oldsha1234567890" if calls.count(args) == 1 else "newsha1234567890"
            return GitCommandResult(["git", *args], 0, f"{sha}\n", "")
        if args == ["pull", "--ff-only"]:
            return GitCommandResult(["git", *args], 0, "Fast-forward\n", "")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(updater, "get_update_status", lambda *a, **k: status)
    monkeypatch.setattr(updater, "run_git_command", fake_git)

    result = pull_latest(tmp_path)

    assert result.success is True
    assert result.old_commit == "oldsha1234567890"
    assert result.new_commit == "newsha1234567890"
    assert ["pull", "--ff-only"] in calls
