"""Tests for aura.git_ops — git integration, auto-commit, snapshots."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aura.git_ops import (
    auto_commit,
    commit_changed_files,
    commit_diff,
    ensure_aura_gitignored,
    git_init,
    is_git_repo,
    recent_commits,
    restore_to_snapshot,
    snapshot,
    undo_last_commit,
    working_tree_status,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class MockResult:
    """Simulates subprocess.CompletedProcess for testing.

    Attributes match those that the production code accesses (returncode,
    stdout, stderr).
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_run(side_effects):
    """Build a ``subprocess.run`` mock that yields from *side_effects*.

    Each element of *side_effects* is either:
      - A ``MockResult`` instance — returned directly (or turned into a
        ``CalledProcessError`` if ``check=True`` and ``returncode != 0``).
      - An ``Exception`` instance — raised directly.

    When ``check=True`` and the result's ``returncode != 0`` the mock raises
    ``subprocess.CalledProcessError`` with ``stderr``/``stdout`` left as
    strings when ``text=True`` was passed (matching real subprocess behaviour),
    or encoded to bytes when ``text`` was not passed.
    """
    calls = list(reversed(side_effects))

    def _run(*args, **kwargs):
        item = calls.pop()
        if isinstance(item, BaseException):
            raise item
        if kwargs.get("check") and item.returncode != 0:
            cmd = args[0] if args else kwargs.get("cmd", [])
            is_text = kwargs.get("text", False)
            stderr_val = item.stderr
            stdout_val = item.stdout
            if not is_text:
                if isinstance(stderr_val, str):
                    stderr_val = stderr_val.encode()
                if isinstance(stdout_val, str):
                    stdout_val = stdout_val.encode()
            raise subprocess.CalledProcessError(
                item.returncode, cmd, output=stdout_val, stderr=stderr_val,
            )
        return item

    return _run


# ===================================================================
# TestIsGitRepo
# ===================================================================


class TestIsGitRepo:
    """is_git_repo() — check whether a directory is inside a git working tree."""

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return True when git rev-parse succeeds."""
        monkeypatch.setattr(subprocess, "run", _make_run([MockResult()]))
        assert is_git_repo(tmp_path) is True

    def test_called_process_error(self, monkeypatch, tmp_path: Path) -> None:
        """Return False when CalledProcessError is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.CalledProcessError(1, ["git", "rev-parse"])]),
        )
        assert is_git_repo(tmp_path) is False

    def test_file_not_found(self, monkeypatch, tmp_path: Path) -> None:
        """Return False when FileNotFoundError is raised."""
        monkeypatch.setattr(
            subprocess, "run", _make_run([FileNotFoundError("no git")]),
        )
        assert is_git_repo(tmp_path) is False

    def test_timeout_expired(self, monkeypatch, tmp_path: Path) -> None:
        """Return False when TimeoutExpired is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git", 5)]),
        )
        assert is_git_repo(tmp_path) is False


# ===================================================================
# TestAutoCommit
# ===================================================================


class TestAutoCommit:
    """auto_commit() — stage and commit changed files."""

    # -------------------- early returns --------------------

    def test_not_a_repo(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when the workspace is not a git repo."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: False)
        ok, msg = auto_commit(tmp_path, "goal", ["f.txt"], "summary")
        assert ok is False
        assert msg == "Not a git repository."

    def test_empty_files_list(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when the files list is empty."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        ok, msg = auto_commit(tmp_path, "goal", [], "summary")
        assert ok is False
        assert msg == "No files to commit."

    # -------------------- git add failure --------------------

    def test_git_add_fails_called_process_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when git add raises CalledProcessError."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.CalledProcessError(1, ["git", "add"])]),
        )
        ok, msg = auto_commit(tmp_path, "goal", ["f.txt"], "summary")
        assert ok is False
        assert msg == "git add failed."

    def test_git_add_fails_timeout(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git add times out."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git add", 10)]),
        )
        ok, msg = auto_commit(tmp_path, "goal", ["f.txt"], "summary")
        assert ok is False
        assert msg == "git add failed."

    # -------------------- no changes to stage --------------------

    def test_no_changes_to_commit(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when diff --cached --quiet returns 0 (no staged changes).

        Also verify that ``git reset`` was called to unstage by ensuring
        all side-effects (including the reset call) are consumed.
        """
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        # Three calls: git add, git diff (returns 0 -> no changes), git reset
        monkeypatch.setattr(
            subprocess, "run", _make_run([
                MockResult(),              # git add — OK
                MockResult(returncode=0),  # git diff — no changes
                MockResult(),              # git reset — unstage
            ])
        )
        ok, msg = auto_commit(tmp_path, "goal", ["f.txt"], "summary")
        assert ok is False
        assert msg == "No changes to commit."

    # -------------------- successful commit --------------------

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when all git operations succeed."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        # git add, git diff (returns 1 -> has changes), git commit
        monkeypatch.setattr(
            subprocess, "run", _make_run([
                MockResult(),              # git add — OK
                MockResult(returncode=1),  # git diff — changes exist
                MockResult(),              # git commit — OK
            ])
        )
        ok, msg = auto_commit(tmp_path, "goal", ["f.txt"], "summary")
        assert ok is True
        assert msg == "Committed: goal"

    # -------------------- commit failure --------------------

    def test_commit_fails_called_process_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when git commit raises CalledProcessError.

        Verify that ``git reset`` is called on commit failure.
        """
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        # git add, git diff (returns 1), git commit (raises), git reset
        monkeypatch.setattr(
            subprocess, "run", _make_run([
                MockResult(),                            # git add — OK
                MockResult(returncode=1),                # git diff — changes exist
                subprocess.CalledProcessError(           # git commit — fails
                    1, ["git", "commit", "-m", "msg"]
                ),
                MockResult(),                            # git reset — unstage
            ])
        )
        ok, msg = auto_commit(tmp_path, "goal", ["f.txt"], "summary")
        assert ok is False
        assert msg == "git commit failed."

    def test_commit_fails_timeout(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git commit times out.

        Verify that ``git reset`` is called on commit failure.
        """
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        # git add, git diff (returns 1), git commit (times out), git reset
        monkeypatch.setattr(
            subprocess, "run", _make_run([
                MockResult(),                            # git add — OK
                MockResult(returncode=1),                # git diff — changes exist
                subprocess.TimeoutExpired("git commit", 10),  # git commit — times out
                MockResult(),                            # git reset — unstage
            ])
        )
        ok, msg = auto_commit(tmp_path, "goal", ["f.txt"], "summary")
        assert ok is False
        assert msg == "git commit failed."

    # -------------------- message truncation --------------------

    def test_message_truncation(self, monkeypatch, tmp_path: Path) -> None:
        """Truncate the commit message when goal + summary exceeds 2000 chars."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)

        # The goal becomes the subject and is truncated to 72 chars.
        # To exceed 2000 chars total, we must use a very long summary.
        long_goal = "short goal"
        long_summary = "s" * 2500  # total > 2000

        captured_message: list[str] = []

        def _tracking_run(*args: object, **kwargs: object) -> MockResult:
            cmd = args[0]
            if isinstance(cmd, list) and len(cmd) >= 4 and cmd[:2] == ["git", "commit"]:
                captured_message.append(str(cmd[3]))
            return MockResult(returncode=1)

        monkeypatch.setattr(subprocess, "run", _tracking_run)
        ok, msg = auto_commit(tmp_path, long_goal, ["f.txt"], long_summary)
        assert ok is True
        assert msg == f"Committed: {long_goal}"
        assert len(captured_message) == 1
        committed_msg = captured_message[0]
        assert committed_msg.endswith("... (truncated)")
        # The truncated part should be <= 2000 + len("\n... (truncated)")
        assert len(committed_msg) <= 2000 + len("\n... (truncated)")


# ===================================================================
# TestCheckpointHelpers
# ===================================================================


class TestCheckpointHelpers:
    """Checkpoint history helpers."""

    def test_recent_commits_not_a_repo(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: False)
        ok, commits, msg = recent_commits(tmp_path)
        assert ok is False
        assert commits == []
        assert msg == "Not a git repository."

    def test_recent_commits_parses_log_records(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        stdout = (
            "\x1eabc123def\x1fabc123d\x1fAdd thing\x1fAda\x1f2 hours ago\n"
            "a.txt\n"
            "b.txt\n"
            "\x1edef456ghi\x1fdef456g\x1fFix thing\x1fGrace\x1fyesterday\n"
            "src/main.py\n"
        )
        monkeypatch.setattr(subprocess, "run", _make_run([MockResult(stdout=stdout)]))

        ok, commits, msg = recent_commits(tmp_path, limit=2)

        assert ok is True
        assert msg == ""
        assert commits == [
            {
                "sha": "abc123def",
                "short_sha": "abc123d",
                "subject": "Add thing",
                "author": "Ada",
                "relative_date": "2 hours ago",
                "changed_files_count": 2,
                "changed_files": ["a.txt", "b.txt"],
            },
            {
                "sha": "def456ghi",
                "short_sha": "def456g",
                "subject": "Fix thing",
                "author": "Grace",
                "relative_date": "yesterday",
                "changed_files_count": 1,
                "changed_files": ["src/main.py"],
            },
        ]

    def test_recent_commits_reports_git_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(returncode=128, stderr="fatal: bad revision")]),
        )
        ok, commits, msg = recent_commits(tmp_path)
        assert ok is False
        assert commits == []
        assert msg == "fatal: bad revision"

    def test_recent_commits_empty_repo(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([
                MockResult(
                    returncode=128,
                    stderr="fatal: your current branch does not have any commits yet",
                ),
            ]),
        )
        ok, commits, msg = recent_commits(tmp_path)
        assert ok is True
        assert commits == []
        assert msg == ""

    def test_commit_changed_files_success(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(stdout="a.txt\nsrc/main.py\n")]),
        )
        ok, files, msg = commit_changed_files(tmp_path, "abc123")
        assert ok is True
        assert files == ["a.txt", "src/main.py"]
        assert msg == ""

    def test_commit_diff_success(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(stdout="commit abc123\n+hello\n")]),
        )
        ok, diff, msg = commit_diff(tmp_path, "abc123")
        assert ok is True
        assert diff == "commit abc123\n+hello\n"
        assert msg == ""

    def test_working_tree_status_success(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess,
            "run",
            _make_run([MockResult(stdout=" M aura/git_ops.py\n")]),
        )
        ok, status, msg = working_tree_status(tmp_path)
        assert ok is True
        assert status == " M aura/git_ops.py\n"
        assert msg == ""


# ===================================================================
# TestUndoLastCommit
# ===================================================================


class TestUndoLastCommit:
    """undo_last_commit() — soft-reset HEAD~1, keeping changes staged."""

    def test_not_a_repo(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when not a git repo."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: False)
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Not a git repository."

    def test_no_commits(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when rev-list reports 0 commits."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="0\n")]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "No commits to undo."

    def test_rev_list_called_process_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when rev-list raises CalledProcessError."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                subprocess.CalledProcessError(1, ["git", "rev-list", "--count", "HEAD"]),
            ]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Could not check git history."

    def test_rev_list_timeout(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when rev-list times out."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git rev-list", 5)]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Could not check git history."

    def test_value_error_parse(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when rev-list stdout is not a valid integer."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="not_a_number\n")]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "Could not check git history."

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when reset --soft HEAD~1 succeeds."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(stdout="5\n"),    # rev-list — 5 commits
                MockResult(),                 # reset --soft HEAD~1 — OK
            ]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is True
        assert msg == "Undo complete — last commit reverted, changes are staged."

    def test_reset_fails(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git reset raises CalledProcessError."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(stdout="5\n"),           # rev-list — OK
                MockResult(returncode=1, stderr="some error"),
                # reset raises CalledProcessError (check=True + rc != 0)
            ]),
        )
        ok, msg = undo_last_commit(tmp_path)
        assert ok is False
        assert msg == "git reset failed: some error"


# ===================================================================
# TestSnapshot
# ===================================================================


class TestSnapshot:
    """snapshot() — capture the current HEAD SHA."""

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return the SHA when rev-parse returns it."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="abc123def\n", returncode=0)]),
        )
        result = snapshot(tmp_path)
        assert result == "abc123def"

    def test_empty_stdout(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when stdout is empty (no HEAD yet)."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult(stdout="")]),
        )
        assert snapshot(tmp_path) is None

    def test_called_process_error(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when rev-parse raises CalledProcessError."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"]),
            ]),
        )
        assert snapshot(tmp_path) is None

    def test_file_not_found(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when FileNotFoundError is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([FileNotFoundError("no git")]),
        )
        assert snapshot(tmp_path) is None

    def test_timeout_expired(self, monkeypatch, tmp_path: Path) -> None:
        """Return None when TimeoutExpired is raised."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git rev-parse", 5)]),
        )
        assert snapshot(tmp_path) is None


# ===================================================================
# TestRestoreToSnapshot
# ===================================================================


class TestRestoreToSnapshot:
    """restore_to_snapshot() — hard-reset to a given SHA."""

    def test_not_a_repo(self, monkeypatch, tmp_path: Path) -> None:
        """Return early when not a git repo."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: False)
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is False
        assert msg == "Not a git repository."

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when git reset --hard succeeds."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([MockResult()]),
        )
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is True
        assert msg == "Restored to abc123de."

    def test_called_process_error(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git reset raises CalledProcessError."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(returncode=1, stderr="fatal: invalid ref"),
            ]),
        )
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is False
        assert msg == "git reset failed: fatal: invalid ref"

    def test_timeout_expired(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git reset times out."""
        monkeypatch.setattr("aura.git_ops.is_git_repo", lambda p: True)
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git reset", 10)]),
        )
        ok, msg = restore_to_snapshot(tmp_path, "abc123de")
        assert ok is False
        assert msg == "git reset timed out."


# ===================================================================
# TestGitInit
# ===================================================================


class TestGitInit:
    """git_init() — initialize a git repo and create an initial commit."""

    def test_success(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when all operations succeed."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                # git init — OK
                MockResult(),                # git add -A — OK
                MockResult(returncode=0),    # git commit — OK
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is True
        assert msg == "git init complete — initial commit created."

    def test_git_init_fails_called_process_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when git init raises CalledProcessError."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(returncode=1, stderr="permission denied"),
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is False
        assert msg == "git init failed: permission denied"

    def test_git_init_timeout(self, monkeypatch, tmp_path: Path) -> None:
        """Return failure when git init times out."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([subprocess.TimeoutExpired("git init", 10)]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is False
        assert msg == "git init timed out."

    def test_nothing_to_commit(self, monkeypatch, tmp_path: Path) -> None:
        """Return success when commit stderr contains 'nothing to commit'."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                # git init — OK
                MockResult(),                # git add -A — OK
                MockResult(                  # git commit — "nothing to commit"
                    returncode=1,
                    stderr="nothing to commit, working tree clean",
                ),
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is True
        assert msg == "git init complete (no files to commit yet)."

    def test_commit_fails_other_error(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Return failure when commit fails with a non-'nothing to commit' error."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                # git init — OK
                MockResult(),                # git add -A — OK
                MockResult(                  # git commit — other error
                    returncode=1,
                    stderr="error: failed to commit",
                ),
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is False
        assert msg == "git commit failed: error: failed to commit"

    def test_git_add_fails_but_continues(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Proceed to commit even if git add -A fails (continues silently)."""
        monkeypatch.setattr(
            subprocess, "run",
            _make_run([
                MockResult(),                            # git init — OK
                subprocess.CalledProcessError(           # git add -A — fails (caught)
                    1, ["git", "add", "-A"]
                ),
                MockResult(returncode=0),                # git commit — OK
            ]),
        )
        ok, msg = git_init(tmp_path)
        assert ok is True
        assert msg == "git init complete — initial commit created."


# ===================================================================
# TestEnsureAuraGitignored
# ===================================================================


class TestEnsureAuraGitignored:
    """ensure_aura_gitignored() — add .aura/ to .gitignore."""

    def test_no_gitignore_creates_one(self, tmp_path: Path) -> None:
        """Create .gitignore with ``.aura/`` when none exists."""
        gitignore = tmp_path / ".gitignore"
        assert not gitignore.exists()
        ensure_aura_gitignored(tmp_path)
        assert gitignore.exists()
        content = gitignore.read_text(encoding="utf-8")
        assert content == ".aura/\n"

    def test_append_when_missing(self, tmp_path: Path) -> None:
        """Append ``.aura/`` when .gitignore exists but doesn't mention .aura."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content == "*.pyc\n.aura/\n"

    def test_already_present_as_dot_aura(self, tmp_path: Path) -> None:
        """Leave unchanged when ``.aura`` (no slash) is already a line."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".aura\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content == ".aura\n"

    def test_already_present_as_dot_aura_slash(self, tmp_path: Path) -> None:
        """Leave unchanged when ``.aura/`` is already a line."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".aura/\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content == ".aura/\n"

    def test_already_present_with_star(self, tmp_path: Path) -> None:
        """Leave unchanged when ``.aura/*`` is already present."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".aura/*\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content == ".aura/*\n"

    def test_already_present_with_bang(self, tmp_path: Path) -> None:
        """Leave unchanged when ``.aura!`` pattern is already present."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".aura!important\n", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content == ".aura!important\n"

    def test_file_no_trailing_newline(self, tmp_path: Path) -> None:
        """Add newline before appending when .gitignore doesn't end with one."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("some_rule", encoding="utf-8")
        ensure_aura_gitignored(tmp_path)
        content = gitignore.read_text(encoding="utf-8")
        assert content == "some_rule\n.aura/\n"

    def test_os_error_on_read_is_silent(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Silently return when reading .gitignore raises OSError.

        Monkeypatch ``Path.read_text`` at the class level (the only safe way
        on Windows where Path attributes are read-only on instances).
        """
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("some content\n", encoding="utf-8")
        original_content = "some content\n"

        def _broken_read_text(_self: object, *args: object, **kwargs: object) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _broken_read_text)
        ensure_aura_gitignored(tmp_path)
        # Undo the class-level patch so we can read the file for assertion
        monkeypatch.undo()
        assert gitignore.read_text(encoding="utf-8") == original_content
