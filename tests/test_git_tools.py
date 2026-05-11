"""Exhaustive unit tests for all 8 git command wrappers in git_tools.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aura.conversation.tools.git_tools import (
    git_branch_list,
    git_diff,
    git_log,
    git_log_file,
    git_show,
    git_stash_list,
    git_stash_show,
    git_status,
)
from tests.helpers import MockResult, _make_run


# ===================================================================
# TestGitStatus
# ===================================================================


class TestGitStatus:
    """git_status() — branch, tracking info, staged/unstaged/untracked files."""

    def test_success_clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Parse a full ## header with tracking, staged, unstaged, and untracked files."""
        stdout = (
            "## main...origin/main [ahead 2, behind 1]\n"
            "M  file.py\n"
            " M file2.py\n"
            "?? new.py\n"
        )
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(stdout=stdout),
                MockResult(stdout="https://github.com/user/repo.git"),
            ]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is True
        assert result["branch"] == "main"
        assert result["tracking"] == "origin/main"
        assert result["ahead"] == 2
        assert result["behind"] == 1
        assert result["staged"] == ["file.py"]
        assert result["unstaged"] == ["file2.py"]
        assert result["untracked"] == ["new.py"]
        assert result["clean"] is False
        assert result["remote_url"] == "https://github.com/user/repo.git"

    def test_success_clean_repo(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Branch with no tracking info => clean=True, empty lists."""
        stdout = "## main\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is True
        assert result["branch"] == "main"
        assert result["tracking"] is None
        assert result["ahead"] == 0
        assert result["behind"] == 0
        assert result["staged"] == []
        assert result["unstaged"] == []
        assert result["untracked"] == []
        assert result["clean"] is True

    def test_rename_parsing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A staged rename line => the new name is listed in staged."""
        stdout = "## main\nR  old.py -> new.py\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is True
        assert result["staged"] == ["new.py"]

    def test_quoted_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A path with spaces is unwrapped from surrounding double-quotes."""
        stdout = '## main\n M "path with spaces.py"\n'
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is True
        assert result["unstaged"] == ["path with spaces.py"]

    def test_empty_repo_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """No ## header => falls back to git branch --show-current."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(stdout=""),          # git status — no ## header
                MockResult(stdout="main\n"),    # git branch --show-current
            ]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is True
        assert result["branch"] == "main"

    def test_not_a_repo(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Non-zero returncode => error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stderr="fatal: not a git repository")]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is False
        assert "Not a git repository" in result["error"]

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git status timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git status", 10)]),
        )
        result = git_status(tmp_path)
        assert result["ok"] is False
        assert "timed out" in result["error"]


# ===================================================================
# TestGitLog
# ===================================================================


class TestGitLog:
    """git_log() — recent commit history with oneline format."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Parse two commits from git log output."""
        stdout = "abc123 First commit\ndef456 Second commit\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_log(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["commits"][0] == {"hash": "abc123", "message": "First commit"}
        assert result["commits"][1] == {"hash": "def456", "message": "Second commit"}

    def test_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Empty stdout => empty commits list."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="")]),
        )
        result = git_log(tmp_path)
        assert result["ok"] is True
        assert result["commits"] == []
        assert result["count"] == 0

    def test_with_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify that a path argument appends -- <path> to the command."""
        captured_cmds: list[list[str]] = []

        def _capturing_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured_cmds.append(list(cmd))
            return MockResult(stdout="abc123 msg\n")

        monkeypatch.setattr(subprocess, "run", _capturing_run)
        result = git_log(tmp_path, path="some/file.py")
        assert result["ok"] is True
        assert "--" in captured_cmds[0]
        assert "some/file.py" in captured_cmds[0]

    def test_non_zero_returncode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Non-zero returncode => error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stderr="fatal: not a git repo")]),
        )
        result = git_log(tmp_path)
        assert result["ok"] is False

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_log(tmp_path)
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git log timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git log", 10)]),
        )
        result = git_log(tmp_path)
        assert result["ok"] is False
        assert "timed out" in result["error"]


# ===================================================================
# TestGitDiff
# ===================================================================


class TestGitDiff:
    """git_diff() — unstaged or staged diff with optional path filtering."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Return diff output, not truncated, ok=True."""
        stdout = "diff --git a/file.py b/file.py\nindex abc..def 100644\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-hello\n+hello world\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_diff(tmp_path)
        assert result["ok"] is True
        assert result["diff"] == stdout
        assert result["truncated"] is False

    def test_staged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify --cached is in the command when staged=True."""
        captured_cmds: list[list[str]] = []

        def _capturing_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured_cmds.append(list(cmd))
            return MockResult(stdout="diff...\n")

        monkeypatch.setattr(subprocess, "run", _capturing_run)
        result = git_diff(tmp_path, staged=True)
        assert result["ok"] is True
        assert "--cached" in captured_cmds[0]

    def test_with_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify -- <path> is appended when path is given."""
        captured_cmds: list[list[str]] = []

        def _capturing_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured_cmds.append(list(cmd))
            return MockResult(stdout="diff...\n")

        monkeypatch.setattr(subprocess, "run", _capturing_run)
        result = git_diff(tmp_path, path="some/file.py")
        assert result["ok"] is True
        assert "--" in captured_cmds[0]
        assert "some/file.py" in captured_cmds[0]

    def test_truncation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Output larger than 200KB is truncated with a marker."""
        # 300 000 'x' chars = ~293 KB > 200 KB
        large_output = "x" * 300_000
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=large_output)]),
        )
        result = git_diff(tmp_path)
        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["diff"].endswith("[truncated at 200KB]\n")

    def test_non_zero_returncode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Non-zero returncode => error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stderr="fatal: bad revision")]),
        )
        result = git_diff(tmp_path)
        assert result["ok"] is False

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_diff(tmp_path)
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git diff timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git diff", 10)]),
        )
        result = git_diff(tmp_path)
        assert result["ok"] is False
        assert "timed out" in result["error"]


# ===================================================================
# TestGitShow
# ===================================================================


class TestGitShow:
    """git_show() — full diff and metadata for a specific commit."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Return the fuller format output, not truncated."""
        stdout = (
            "commit abc123def\n"
            "Author:     Test User\n"
            "AuthorDate: Mon Jan 1 12:00:00 2024 +0000\n"
            "Commit:     Test User\n"
            "CommitDate: Mon Jan 1 12:00:00 2024 +0000\n"
            "\n"
            "    My commit message\n"
            "\ndiff --git a/file.py b/file.py\n"
        )
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_show(tmp_path, "abc123def")
        assert result["ok"] is True
        assert result["output"] == stdout
        assert result["truncated"] is False

    def test_truncation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Output larger than 200KB is truncated."""
        large_output = "x" * 300_000
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=large_output)]),
        )
        result = git_show(tmp_path, "abc123def")
        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["output"].endswith("[truncated at 200KB]\n")

    def test_invalid_commit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Returncode 128 with bad revision stderr => error."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=128, stderr="fatal: bad revision 'xyz'")]),
        )
        result = git_show(tmp_path, "xyz")
        assert result["ok"] is False
        assert "fatal" in result["error"]

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_show(tmp_path, "abc")
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git show timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git show", 30)]),
        )
        result = git_show(tmp_path, "abc")
        assert result["ok"] is False
        assert "timed out" in result["error"]


# ===================================================================
# TestGitLogFile
# ===================================================================


class TestGitLogFile:
    """git_log_file() — commit history for a single file with --follow."""

    def test_success_4_fields(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Parse commits with all 4 fields (hash, message, author, date)."""
        stdout = (
            "abc||First commit||Author One||2024-01-01\n"
            "def||Second commit||Author Two||2024-01-02\n"
        )
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_log_file(tmp_path, "some/file.py")
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["commits"][0] == {
            "hash": "abc", "message": "First commit", "author": "Author One", "date": "2024-01-01",
        }
        assert result["commits"][1] == {
            "hash": "def", "message": "Second commit", "author": "Author Two", "date": "2024-01-02",
        }

    def test_success_2_fields(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Parse commits with only 2 fields (hash, message) — author/date default to empty."""
        stdout = "abc||my message\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_log_file(tmp_path, "f.txt")
        assert result["ok"] is True
        assert result["count"] == 1
        assert result["commits"][0] == {
            "hash": "abc", "message": "my message", "author": "", "date": "",
        }

    def test_success_1_field(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Parse commits with only 1 field (hash) — others default to empty."""
        stdout = "abc\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_log_file(tmp_path, "f.txt")
        assert result["ok"] is True
        assert result["count"] == 1
        assert result["commits"][0] == {
            "hash": "abc", "message": "", "author": "", "date": "",
        }

    def test_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Empty stdout => empty commits list."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="")]),
        )
        result = git_log_file(tmp_path, "f.txt")
        assert result["ok"] is True
        assert result["commits"] == []
        assert result["count"] == 0

    def test_non_zero_returncode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Non-zero returncode => error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stderr="fatal: not a git repo")]),
        )
        result = git_log_file(tmp_path, "f.txt")
        assert result["ok"] is False

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_log_file(tmp_path, "f.txt")
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git log_file timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git log", 10)]),
        )
        result = git_log_file(tmp_path, "f.txt")
        assert result["ok"] is False
        assert "timed out" in result["error"]


# ===================================================================
# TestGitBranchList
# ===================================================================


class TestGitBranchList:
    """git_branch_list() — local branches with tracking info."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Branch with upstream and ahead/behind info."""
        stdout = "main|*|origin/main|[ahead 1]\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_branch_list(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 1
        branch = result["branches"][0]
        assert branch["name"] == "main"
        assert branch["current"] is True
        assert branch["upstream"] == "origin/main"
        assert branch["ahead_behind"] == "[ahead 1]"

    def test_no_upstream(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Branch with no upstream => upstream and ahead_behind are None."""
        stdout = "feature|||\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_branch_list(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 1
        branch = result["branches"][0]
        assert branch["name"] == "feature"
        assert branch["current"] is False
        assert branch["upstream"] is None
        assert branch["ahead_behind"] is None

    def test_multiple_branches(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Three branches: one current, two non-current."""
        stdout = (
            "main|*|origin/main|[ahead 1]\n"
            "feature||origin/main|\n"
            "bugfix|||\n"
        )
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_branch_list(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 3
        assert result["branches"][0]["name"] == "main"
        assert result["branches"][0]["current"] is True
        assert result["branches"][1]["name"] == "feature"
        assert result["branches"][1]["current"] is False
        assert result["branches"][2]["name"] == "bugfix"
        assert result["branches"][2]["current"] is False

    def test_non_zero_returncode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Non-zero returncode => error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stderr="fatal: not a git repo")]),
        )
        result = git_branch_list(tmp_path)
        assert result["ok"] is False

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_branch_list(tmp_path)
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git branch_list timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git branch", 10)]),
        )
        result = git_branch_list(tmp_path)
        assert result["ok"] is False
        assert "timed out" in result["error"]


# ===================================================================
# TestGitStashList
# ===================================================================


class TestGitStashList:
    """git_stash_list() — list all stashes."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Parse two stashes with index, context, and message."""
        stdout = (
            "stash@{0}: WIP on main: fix bug\n"
            "stash@{1}: WIP on feature: add feature\n"
        )
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_stash_list(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["stashes"][0] == {"index": "0", "context": "WIP on main", "message": "fix bug"}
        assert result["stashes"][1] == {"index": "1", "context": "WIP on feature", "message": "add feature"}

    def test_unparseable_line(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A line that doesn't match the stash regex gets a 'raw' field."""
        stdout = "some random unparseable line\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_stash_list(tmp_path)
        assert result["ok"] is True
        assert result["count"] == 1
        assert result["stashes"][0] == {"raw": "some random unparseable line"}

    def test_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Empty stdout => empty stashes list."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="")]),
        )
        result = git_stash_list(tmp_path)
        assert result["ok"] is True
        assert result["stashes"] == []
        assert result["count"] == 0

    def test_non_zero_returncode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Non-zero returncode => error dict."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stderr="fatal: not a git repo")]),
        )
        result = git_stash_list(tmp_path)
        assert result["ok"] is False

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_stash_list(tmp_path)
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git stash list timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git stash list", 10)]),
        )
        result = git_stash_list(tmp_path)
        assert result["ok"] is False
        assert "timed out" in result["error"]


# ===================================================================
# TestGitStashShow
# ===================================================================


class TestGitStashShow:
    """git_stash_show() — diff of a specific stash."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Return the diff output, not truncated."""
        stdout = "diff --git a/file.py b/file.py\nindex abc..def 100644\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=stdout)]),
        )
        result = git_stash_show(tmp_path)
        assert result["ok"] is True
        assert result["diff"] == stdout
        assert result["truncated"] is False

    def test_index_param(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify that index=2 results in stash@{2} in the command."""
        captured_cmds: list[list[str]] = []

        def _capturing_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0] if args else kwargs.get("cmd", [])
            captured_cmds.append(list(cmd))
            return MockResult(stdout="diff...\n")

        monkeypatch.setattr(subprocess, "run", _capturing_run)
        result = git_stash_show(tmp_path, index=2)
        assert result["ok"] is True
        assert "stash@{2}" in captured_cmds[0]

    def test_truncation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Output larger than 200KB is truncated."""
        large_output = "x" * 300_000
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout=large_output)]),
        )
        result = git_stash_show(tmp_path)
        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["diff"].endswith("[truncated at 200KB]\n")

    def test_non_existent_stash(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Non-zero returncode for a non-existent stash => error."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(returncode=1, stderr="fatal: no stashes found")]),
        )
        result = git_stash_show(tmp_path, index=99)
        assert result["ok"] is False

    def test_file_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileNotFoundError => git not installed error message."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        result = git_stash_show(tmp_path)
        assert result["ok"] is False
        assert "git is not installed" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """TimeoutExpired => git stash show timed out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git stash show", 30)]),
        )
        result = git_stash_show(tmp_path)
        assert result["ok"] is False
        assert "timed out" in result["error"]
